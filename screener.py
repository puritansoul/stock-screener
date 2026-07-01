"""
Point-in-Time Multi-Factor S&P 500 Screener — Backtest v3

Merged with your separate screener's best ideas:
  Hard filters (applied before ranking — eliminates junk before scoring):
    Market Cap  > $1B
    ROIC        >= 8%        (EBIT / Invested Capital)
    Piotroski   >= 5/9       (only when >= 5 signals computable)
    Debt/EBITDA <= 3.5x
    EV/EBITDA   <= 25x

  Scoring factors (weighted composite rank):
    Momentum          20%   12-1 month price momentum
    ROIC              20%   EBIT / (Equity + LTD)
    FCF / EV          15%   (OpCF - Capex) / EV
    EV / EBITDA       10%   lower = better
    Piotroski         10%   9-signal quality score
    Low Accruals      10%   (NetIncome - OpCF) / Assets
    EPS Surprise      10%   YoY EPS change
    Revenue Growth     5%   3-year CAGR

Data:
  Prices      : yfinance  (free)
  Fundamentals: SEC EDGAR XBRL Frames API  (free, public domain, point-in-time)
"""

from __future__ import annotations

import json
import time
import warnings
from io import StringIO
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ─────────────────────────────────────────────────────────────────────
BACKTEST_START   = "2015-01-01"
BACKTEST_END     = "2024-12-31"
TOP_DECILE_PCT   = 0.10
REBALANCE_MONTHS = {3, 6, 9, 12}
FILING_LAG_DAYS  = 75
SEC_DELAY_S      = 0.12
CACHE_DIR        = Path("/Users/vishalgupta/claude/edgar_cache")

# Hard filter thresholds (NaN = passes, benefit of doubt)
F_MKTCAP_MIN      = 1e9      # $1B
F_ROIC_MIN        = 0.08     # 8%
F_PIOTROSKI_MIN   = 5        # out of 9
F_DEBT_EBITDA_MAX = 3.5
F_EV_EBITDA_MAX   = 25.0

# Scoring weights (must sum to 1.0)
WEIGHTS = {
    "momentum":    0.20,
    "roic":        0.20,
    "fcf_ev":      0.15,
    "ev_ebitda":   0.10,
    "piotroski":   0.10,
    "accruals":    0.10,
    "eps_surprise":0.10,
    "rev_growth":  0.05,
}

SEC_HEADERS  = {"User-Agent": "QuantResearch screener@research.com", "Accept-Encoding": "gzip, deflate"}
WIKI_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Balance sheet instant items — use quarterly instant frames CY{year}Q{quarter}I
CONCEPTS_QI: dict[str, tuple[str, str]] = {
    "assets":      ("Assets",                                "USD"),
    "ltd":         ("LongTermDebt",                          "USD"),
    "cash":        ("CashAndCashEquivalentsAtCarryingValue", "USD"),
    "shares":      ("CommonStockSharesOutstanding",          "shares"),
    "equity":      ("StockholdersEquity",                    "USD"),
    "curr_assets": ("AssetsCurrent",                         "USD"),
    "curr_liab":   ("LiabilitiesCurrent",                    "USD"),
}

# Flow / income-statement items — use annual frames CY{year} for much better coverage
CONCEPTS_ANN: dict[str, tuple[str, str]] = {
    "gross_profit": ("GrossProfit",                                "USD"),
    "net_income":   ("NetIncomeLoss",                              "USD"),
    "op_cf":        ("NetCashProvidedByUsedInOperatingActivities", "USD"),
    "ebit":         ("OperatingIncomeLoss",                        "USD"),
    "eps":          ("EarningsPerShareDiluted",                    "USD%2Fshares"),
    "revenue":      ("Revenues",                                   "USD"),
    "capex":        ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
    "dna":          ("DepreciationDepletionAndAmortization",       "USD"),
}

# Combined legacy dict kept for any code that still references CONCEPTS
CONCEPTS: dict[str, tuple[str, str, bool]] = {
    **{k: (v[0], v[1], False) for k, v in CONCEPTS_ANN.items()},
    **{k: (v[0], v[1], True)  for k, v in CONCEPTS_QI.items()},
}

FUND_COLS = [
    "gross_profit", "assets", "net_income", "op_cf", "ebit",
    "ltd", "cash", "shares", "revenue", "capex", "equity",
    "curr_assets", "curr_liab", "dna",
]

# ── Cache ──────────────────────────────────────────────────────────────────────

def _cpath(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"

def load_cache(key: str):
    p = _cpath(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None

def save_cache(key: str, data) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cpath(key).write_text(json.dumps(data, default=str))

# ── SEC EDGAR ──────────────────────────────────────────────────────────────────

def get_ticker_cik_map() -> dict[str, str]:
    cached = load_cache("ticker_cik_map")
    if cached:
        return cached
    resp    = requests.get("https://www.sec.gov/files/company_tickers.json",
                           headers={**SEC_HEADERS, "Host": "www.sec.gov"}, timeout=30)
    data    = resp.json()
    mapping = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    save_cache("ticker_cik_map", mapping)
    print(f"  SEC CIK map: {len(mapping)} companies")
    return mapping


def _fetch_xbrl_generic(url: str, cachekey: str, concept_key: str) -> pd.Series:
    """Shared fetch/cache logic for both quarterly-instant and annual XBRL frames."""
    cached = load_cache(cachekey)
    if cached is not None:
        if not cached:
            return pd.Series(dtype=float, name=concept_key)
        df = pd.DataFrame(cached)
        df["cik"] = df["cik"].astype(str).str.zfill(10)
        return df.set_index("cik")["val"].astype(float).rename(concept_key)

    time.sleep(SEC_DELAY_S)
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
        if resp.status_code == 404:
            save_cache(cachekey, [])
            return pd.Series(dtype=float, name=concept_key)
        resp.raise_for_status()
        raw    = resp.json()
        data   = raw.get("data", [])
        if not data:
            save_cache(cachekey, [])
            return pd.Series(dtype=float, name=concept_key)
        fields = raw.get("fields", ["cik", "entityName", "val"])
        df     = pd.DataFrame(data, columns=fields)
        df["cik"] = df["cik"].astype(str).str.zfill(10)
        result = df.drop_duplicates("cik")[["cik", "val"]]
        save_cache(cachekey, result.to_dict("records"))
        return result.set_index("cik")["val"].astype(float).rename(concept_key)
    except Exception as e:
        print(f"    EDGAR {concept_key} ({url.split('/')[-1]}): {e}")
        save_cache(cachekey, [])
        return pd.Series(dtype=float, name=concept_key)


def fetch_xbrl_frame(concept_key: str, year: int, quarter: int) -> pd.Series:
    """Fetch a quarterly-instant (CY{year}Q{quarter}I) XBRL frame for balance sheet items."""
    tag, unit = CONCEPTS_QI[concept_key]
    period   = f"CY{year}Q{quarter}I"
    cachekey = f"xbrl_{concept_key}_{period}"
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json"
    return _fetch_xbrl_generic(url, cachekey, concept_key)


def fetch_xbrl_annual(concept_key: str, year: int) -> pd.Series:
    """Fetch an annual (CY{year}) XBRL frame for flow/income-statement items."""
    tag, unit = CONCEPTS_ANN[concept_key]
    period   = f"CY{year}"
    cachekey = f"xbrl_ann_{concept_key}_{year}"
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json"
    return _fetch_xbrl_generic(url, cachekey, concept_key)


def get_available_quarter(as_of: pd.Timestamp, lag: int = FILING_LAG_DAYS) -> tuple[int, int]:
    cutoff    = as_of - pd.Timedelta(days=lag)
    q_ends    = pd.date_range("2009-01-01", str(as_of.year + 1), freq="Q")
    available = [(d.year, (d.month - 1) // 3 + 1) for d in q_ends if d <= cutoff]
    return available[-1] if available else (2014, 4)


def get_available_annual_year(as_of: pd.Timestamp, lag: int = FILING_LAG_DAYS) -> int:
    """
    Return the most recent fiscal year whose Dec 31 10-K would be available
    by as_of date given the filing lag. A Dec 31 year-end 10-K is considered
    available 75+ days after Dec 31 of that year.
    """
    cutoff = as_of - pd.Timedelta(days=lag)
    year = cutoff.year
    dec31 = pd.Timestamp(f"{year}-12-31")
    if dec31 > cutoff:
        year -= 1
    return max(year, 2009)


def yq_offset(yq: tuple[int, int], years: int) -> tuple[int, int]:
    return (yq[0] - years, yq[1])

# ── Prices ─────────────────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    url  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=WIKI_HEADERS, timeout=15).text
    return pd.read_html(StringIO(html))[0]["Symbol"].str.replace(".", "-", regex=False).tolist()


def fetch_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    raw   = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=True, threads=True)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    return close.dropna(axis=1, how="all")

# ── Factor computation ─────────────────────────────────────────────────────────

def build_fundamental_df(
    frames: dict,
    c2t: dict,
    universe: set,
    cols: list,
    ann_frames: dict | None = None,
    ann_cols: list | None = None,
) -> pd.DataFrame:
    """
    Build a ticker-indexed fundamental DataFrame.

    frames    / cols     : quarterly-instant data (balance sheet)
    ann_frames / ann_cols: annual flow data — merged on ticker index
    """
    built = {}

    def _add(src_frames, keys):
        for key in keys:
            s = src_frames.get(key, pd.Series(dtype=float))
            if s.empty:
                continue
            s = s.copy()
            s.index = [c2t.get(c) for c in s.index]
            s = s[s.index.notna()]
            built[key] = s

    _add(frames, cols)
    if ann_frames is not None and ann_cols is not None:
        _add(ann_frames, ann_cols)

    df = pd.DataFrame(built)
    return df[df.index.isin(universe)]


def pct_rank(s: pd.Series, higher_is_better: bool = True) -> pd.Series:
    r = s.rank(pct=True, na_option="keep")
    return r if higher_is_better else (1 - r)


def _safe(s: pd.Series) -> pd.Series:
    return s.replace([np.inf, -np.inf], np.nan)


def compute_roic(fund: pd.DataFrame) -> pd.Series:
    """EBIT / (Equity + LTD). Falls back to EBIT/Assets when IC <= 0."""
    ic = fund.get("equity", pd.Series(dtype=float)).add(
        fund.get("ltd", pd.Series(dtype=float)).fillna(0), fill_value=np.nan)
    ebit = fund.get("ebit", pd.Series(dtype=float))
    # For negative/tiny IC (heavy buyback cos), use assets as denominator
    ic_safe = ic.where(ic > fund["assets"].clip(lower=1) * 0.05,
                       other=fund["assets"])
    return _safe(ebit / ic_safe.clip(lower=1))


def compute_fcf_ev(fund: pd.DataFrame, price_snap: pd.Series) -> pd.Series:
    """(OpCF − Capex) / EV. Capex tag is cash paid (positive), so we subtract."""
    op_cf = fund.get("op_cf",  pd.Series(dtype=float))
    capex = fund.get("capex",  pd.Series(dtype=float)).fillna(0).abs()
    fcf   = op_cf - capex
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    ev      = mkt_cap + fund["ltd"].fillna(0) - fund["cash"].fillna(0)
    return _safe(fcf / ev.clip(lower=1)).where(ev > 0)


def compute_ev_ebitda(fund: pd.DataFrame, price_snap: pd.Series) -> pd.Series:
    ebitda  = fund.get("ebit", pd.Series(dtype=float)) + \
              fund.get("dna",  pd.Series(dtype=float)).fillna(0)
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    ev      = mkt_cap + fund["ltd"].fillna(0) - fund["cash"].fillna(0)
    return _safe(ev / ebitda.clip(lower=1)).where(ebitda > 0)


def compute_debt_ebitda(fund: pd.DataFrame) -> pd.Series:
    ebitda = fund.get("ebit", pd.Series(dtype=float)) + \
             fund.get("dna",  pd.Series(dtype=float)).fillna(0)
    return _safe(fund["ltd"] / ebitda.clip(lower=1)).where(ebitda > 0)


def compute_piotroski(fund: pd.DataFrame, fund_yago: pd.DataFrame) -> pd.Series:
    """
    9-point Piotroski F-score. Missing data signals are excluded from count.
    Returns raw sum 0-9; also returns count of computable signals.
    """
    idx = fund.index
    sig = {}

    def reindex(df, col):
        if col in df.columns:
            return df[col].reindex(idx)
        return pd.Series(np.nan, index=idx)

    # Profitability (4 signals)
    roa = fund["net_income"] / fund["assets"].clip(lower=1)
    sig["roa_pos"]    = (roa > 0).astype(float).where(roa.notna())
    sig["cf_pos"]     = (fund["op_cf"] > 0).astype(float).where(fund["op_cf"].notna()) \
                        if "op_cf" in fund.columns else pd.Series(np.nan, index=idx)
    roa_y = reindex(fund_yago, "net_income") / reindex(fund_yago, "assets").clip(lower=1)
    delta_roa = roa - roa_y
    sig["roa_impr"]   = (delta_roa > 0).astype(float).where(delta_roa.notna())
    accruals = (fund["op_cf"] - fund["net_income"]) / fund["assets"].clip(lower=1) \
               if "op_cf" in fund.columns else pd.Series(np.nan, index=idx)
    sig["accruals"]   = (accruals > 0).astype(float).where(accruals.notna())

    # Leverage/liquidity (3 signals)
    lev   = fund["ltd"] / fund["assets"].clip(lower=1)
    lev_y = reindex(fund_yago, "ltd") / reindex(fund_yago, "assets").clip(lower=1)
    delta_lev = lev - lev_y
    sig["lev_decr"]   = (delta_lev < 0).astype(float).where(delta_lev.notna())

    if "curr_assets" in fund.columns and "curr_liab" in fund.columns:
        cr   = fund["curr_assets"] / fund["curr_liab"].clip(lower=0.01)
        cr_y = reindex(fund_yago, "curr_assets") / reindex(fund_yago, "curr_liab").clip(lower=0.01)
        delta_cr = cr - cr_y
        sig["cr_impr"] = (delta_cr > 0).astype(float).where(delta_cr.notna())
    else:
        sig["cr_impr"] = pd.Series(np.nan, index=idx)

    sh_y  = reindex(fund_yago, "shares")
    delta_sh = fund["shares"] - sh_y
    sig["no_dilution"] = (delta_sh <= sh_y * 0.02).astype(float).where(delta_sh.notna())

    # Operating efficiency (2 signals)
    if "revenue" in fund.columns:
        gm   = fund["gross_profit"] / fund["revenue"].clip(lower=1)
        gm_y = reindex(fund_yago, "gross_profit") / reindex(fund_yago, "revenue").clip(lower=1)
        delta_gm = gm - gm_y
        sig["gm_impr"] = (delta_gm > 0).astype(float).where(delta_gm.notna())

        at   = fund["revenue"] / fund["assets"].clip(lower=1)
        at_y = reindex(fund_yago, "revenue") / reindex(fund_yago, "assets").clip(lower=1)
        delta_at = at - at_y
        sig["at_impr"] = (delta_at > 0).astype(float).where(delta_at.notna())
    else:
        sig["gm_impr"] = pd.Series(np.nan, index=idx)
        sig["at_impr"] = pd.Series(np.nan, index=idx)

    df_sig    = pd.DataFrame(sig, index=idx)
    n_valid   = df_sig.notna().sum(axis=1)
    score_sum = df_sig.sum(axis=1, min_count=1)  # NaN if all NaN
    return score_sum, n_valid


def compute_rev_growth_3y(fund: pd.DataFrame, fund_3yago: pd.DataFrame) -> pd.Series:
    if "revenue" not in fund.columns:
        return pd.Series(dtype=float)
    rev_now = fund["revenue"]
    rev_3y  = fund_3yago["revenue"].reindex(fund.index) if "revenue" in fund_3yago.columns \
              else pd.Series(np.nan, index=fund.index)
    ratio   = (rev_now / rev_3y.clip(lower=1)).where(rev_3y > 0)
    return _safe(ratio ** (1/3) - 1)


def apply_hard_filters(
    fund: pd.DataFrame,
    fund_yago: pd.DataFrame,
    price_snap: pd.Series,
    piotroski_score: pd.Series,
    piotroski_n: pd.Series,
    ev_ebitda: pd.Series,
    debt_ebitda: pd.Series,
    roic: pd.Series,
) -> pd.Series:
    """
    Returns boolean mask. NaN on a filter = passes (benefit of doubt).
    Piotroski filter only applied when >= 5 signals are computable.
    """
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    ev      = mkt_cap + fund["ltd"].fillna(0) - fund["cash"].fillna(0)

    # Market cap filter
    f_mktcap = mkt_cap.isna() | (mkt_cap >= F_MKTCAP_MIN)

    # ROIC filter
    f_roic = roic.isna() | (roic >= F_ROIC_MIN)

    # Piotroski: only apply when >= 5 signals available
    f_piotroski = (
        piotroski_n.isna() |
        (piotroski_n < 5) |
        (piotroski_score >= F_PIOTROSKI_MIN)
    )

    # Debt/EBITDA
    f_debt = debt_ebitda.isna() | (debt_ebitda <= F_DEBT_EBITDA_MAX)

    # EV/EBITDA
    f_ev_ebitda = ev_ebitda.isna() | (ev_ebitda <= F_EV_EBITDA_MAX)

    return f_mktcap & f_roic & f_piotroski & f_debt & f_ev_ebitda


def build_composite(
    tickers: list[str],
    prices: pd.DataFrame,
    fund: pd.DataFrame,
    fund_yago: pd.DataFrame,
    fund_3yago: pd.DataFrame,
    eps_surprise: pd.Series,
    as_of: pd.Timestamp,
) -> pd.Series:
    if fund.empty:
        return pd.Series(dtype=float)

    try:
        price_snap = prices.loc[:as_of].iloc[-1].reindex(fund.index)
    except IndexError:
        price_snap = pd.Series(np.nan, index=fund.index)

    # Compute all factors
    momentum   = _safe(
        (prices.loc[:as_of - pd.DateOffset(months=1)].iloc[-1].reindex(fund.index) -
         prices.loc[:as_of - pd.DateOffset(months=12)].iloc[-1].reindex(fund.index)) /
        prices.loc[:as_of - pd.DateOffset(months=12)].iloc[-1].reindex(fund.index)
    ) if len(prices.loc[:as_of]) > 252 else pd.Series(np.nan, index=fund.index)

    roic          = compute_roic(fund)
    fcf_ev        = compute_fcf_ev(fund, price_snap)
    ev_ebitda     = compute_ev_ebitda(fund, price_snap)
    debt_ebitda   = compute_debt_ebitda(fund)
    accruals      = _safe((fund["net_income"] - fund.get("op_cf", pd.Series(np.nan, index=fund.index)))
                          / fund["assets"].clip(lower=1))
    p_score, p_n  = compute_piotroski(fund, fund_yago)
    rev_growth    = compute_rev_growth_3y(fund, fund_3yago)
    eps_surp      = eps_surprise.reindex(fund.index)

    # Hard filters
    mask = apply_hard_filters(fund, fund_yago, price_snap, p_score, p_n, ev_ebitda, debt_ebitda, roic)

    # Rank each factor
    ranks = pd.DataFrame({
        "momentum":    pct_rank(momentum,  True),
        "roic":        pct_rank(roic,      True),
        "fcf_ev":      pct_rank(fcf_ev,    True),
        "ev_ebitda":   pct_rank(ev_ebitda, False),
        "piotroski":   pct_rank(p_score,   True),
        "accruals":    pct_rank(accruals,  False),
        "eps_surprise":pct_rank(eps_surp,  True),
        "rev_growth":  pct_rank(rev_growth,True),
    }, index=fund.index)

    # Weighted composite: normalize by available weight so missing rank columns
    # (e.g. EPS when annual frame unavailable) don't collapse the score to NaN
    rank_to_factor = {
        "momentum":    "momentum",
        "roic":        "roic",
        "fcf_ev":      "fcf_ev",
        "ev_ebitda":   "ev_ebitda",
        "piotroski":   "piotroski",
        "accruals":    "accruals",
        "eps_surprise":"eps_surprise",
        "rev_growth":  "rev_growth",
    }
    weighted_sum = sum(ranks[col].fillna(0) * WEIGHTS[f] for col, f in rank_to_factor.items())
    weight_avail = sum(ranks[col].notna().astype(float) * WEIGHTS[f] for col, f in rank_to_factor.items())
    composite = _safe(weighted_sum / weight_avail.clip(lower=0.01)).where(weight_avail > 0)

    # Apply filters
    composite = composite.where(mask)
    composite = composite[composite.index.isin(tickers)].dropna()
    return composite.sort_values(ascending=False)

# ── Backtest ───────────────────────────────────────────────────────────────────

def get_rebalance_dates(start: str, end: str) -> list[pd.Timestamp]:
    return [d for d in pd.date_range(start, end, freq="MS") if d.month in REBALANCE_MONTHS]


def run_backtest(
    prices: pd.DataFrame,
    all_fund: dict,
    all_fund_yago: dict,
    all_fund_3yago: dict,
    all_eps: dict,
    start: str,
    end: str,
) -> tuple[pd.Series, pd.Series]:
    rebal_dates = get_rebalance_dates(start, end)
    all_dates   = prices.loc[start:end].index
    tickers     = prices.columns.tolist()

    weights: dict[str, float] = {}
    nav_series: dict          = {}
    nav      = 1.0
    prev_date = None
    rb_idx    = 0

    print(f"  {len(rebal_dates)} rebalance periods …")

    for dt in all_dates:
        if rb_idx < len(rebal_dates) and dt >= rebal_dates[rb_idx]:
            as_of    = rebal_dates[rb_idx]
            rb_idx  += 1
            yq       = get_available_quarter(as_of)
            fund     = all_fund.get(yq, pd.DataFrame())
            f_yago   = all_fund_yago.get(yq, pd.DataFrame())
            f_3yago  = all_fund_3yago.get(yq, pd.DataFrame())
            eps      = all_eps.get(yq, pd.Series(dtype=float))

            if not fund.empty:
                scores  = build_composite(tickers, prices, fund, f_yago, f_3yago, eps, as_of)
                n_sel   = max(1, int(len(scores) * TOP_DECILE_PCT))
                selected= [
                    t for t in scores.head(n_sel).index
                    if t in prices.columns and pd.notna(prices.loc[:dt, t].iloc[-1])
                ]
                if selected:
                    w       = 1.0 / len(selected)
                    weights = {t: w for t in selected}
            print(f"    {as_of.date()} [Q{yq[1]}/{yq[0]}] → {len(weights)} holdings "
                  f"(after hard filters)")

        if prev_date is not None and weights:
            try:
                p_t = prices.loc[dt]
                p_p = prices.loc[prev_date]
                dr  = sum(
                    w * (p_t[t] / p_p[t] - 1)
                    for t, w in weights.items()
                    if t in p_t and t in p_p
                    and pd.notna(p_t[t]) and pd.notna(p_p[t]) and p_p[t] > 0
                )
                nav *= (1 + dr)
            except Exception:
                pass

        nav_series[dt] = nav
        prev_date = dt

    strategy = pd.Series(nav_series, name="Strategy").sort_index()

    spy_raw = yf.download("SPY", start=start, end=end, auto_adjust=True, progress=False)["Close"]
    if isinstance(spy_raw, pd.DataFrame):
        spy_raw = spy_raw.squeeze()
    spy_nav = (spy_raw / spy_raw.iloc[0])
    spy_nav.name = "SPY"

    return strategy.align(spy_nav, join="inner")

# ── Metrics & chart ────────────────────────────────────────────────────────────

def metrics(nav: pd.Series, rf: float = 0.04) -> dict:
    rets  = nav.pct_change().dropna()
    nyrs  = len(nav) / 252
    cagr  = (nav.iloc[-1] / nav.iloc[0]) ** (1 / nyrs) - 1
    vol   = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() * 252 - rf) / vol if vol > 0 else np.nan
    mdd   = ((nav - nav.cummax()) / nav.cummax()).min()
    return {
        "CAGR":       f"{cagr:.1%}",
        "Volatility": f"{vol:.1%}",
        "Sharpe":     f"{sharpe:.2f}",
        "Max DD":     f"{mdd:.1%}",
        "Total Ret":  f"{nav.iloc[-1] - 1:.1%}",
    }


def plot_results(strategy: pd.Series, benchmark: pd.Series, out: str) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12, 14),
                             gridspec_kw={"height_ratios": [3, 1, 1]})
    fig.suptitle("Merged Multi-Factor S&P 500 Screener — Backtest v3\n"
                 "(Hard Filters + ROIC + FCF/EV + Piotroski + Rev Growth)",
                 fontsize=13, fontweight="bold")

    ax1 = axes[0]
    ax1.plot(strategy.index,  strategy.values,  label="Strategy (Top Decile)", lw=2,   color="#1f77b4")
    ax1.plot(benchmark.index, benchmark.values, label="SPY Buy & Hold",        lw=1.5, color="#ff7f0e", ls="--")
    ax1.set_ylabel("NAV (base = 1.0)")
    ax1.legend()
    ax1.set_title("Cumulative NAV — Point-in-Time SEC EDGAR Fundamentals, Hard Quality Filters")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1f}x"))
    ax1.grid(True, alpha=0.3)

    sm = metrics(strategy)
    bm = metrics(benchmark)
    tbl = ax1.table(
        cellText=[[k, sm[k], bm[k]] for k in sm],
        colLabels=["Metric", "Strategy", "SPY"],
        cellLoc="center", loc="upper left",
        bbox=[0.01, 0.38, 0.28, 0.57],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)

    filter_text = (
        f"Hard filters: MktCap≥$1B | ROIC≥{F_ROIC_MIN:.0%} | "
        f"Piotroski≥{F_PIOTROSKI_MIN}/9 | Debt/EBITDA≤{F_DEBT_EBITDA_MAX}x | "
        f"EV/EBITDA≤{F_EV_EBITDA_MAX}x"
    )
    ax1.text(0.01, 0.01, filter_text, transform=ax1.transAxes,
             fontsize=7.5, color="#555", va="bottom")

    ax2 = axes[1]
    sdd = (strategy  - strategy.cummax())  / strategy.cummax()
    bdd = (benchmark - benchmark.cummax()) / benchmark.cummax()
    ax2.fill_between(sdd.index, sdd.values, 0, alpha=0.4, color="#1f77b4", label="Strategy DD")
    ax2.fill_between(bdd.index, bdd.values, 0, alpha=0.3, color="#ff7f0e", label="SPY DD")
    ax2.set_ylabel("Drawdown")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0%}"))
    ax2.legend(fontsize=8)
    ax2.set_title("Drawdown")
    ax2.grid(True, alpha=0.3)

    ax3 = axes[2]
    s12  = strategy.pct_change(252).dropna()
    b12  = benchmark.pct_change(252).dropna()
    exc, b12a = s12.align(b12, join="inner")
    exc -= b12a
    ax3.bar(exc.index, exc.values,
            color=["#2ca02c" if v >= 0 else "#d62728" for v in exc.values],
            alpha=0.7, width=1)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.set_ylabel("Excess Return")
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0%}"))
    ax3.set_title("Rolling 12-Month Alpha vs SPY")
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  Chart → {out}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  Merged Multi-Factor S&P 500 Screener v3")
    print(f"  {BACKTEST_START} → {BACKTEST_END} | Quarterly | Top {TOP_DECILE_PCT:.0%}")
    print(f"  Hard filters: ROIC≥{F_ROIC_MIN:.0%}, Piotroski≥{F_PIOTROSKI_MIN}, "
          f"Debt/EBITDA≤{F_DEBT_EBITDA_MAX}x, EV/EBITDA≤{F_EV_EBITDA_MAX}x")
    print("=" * 65)

    print("\n[1] S&P 500 tickers …")
    tickers = get_sp500_tickers()
    print(f"  {len(tickers)} tickers")

    print("\n[2] Ticker → CIK map …")
    t2cik = get_ticker_cik_map()
    def resolve_cik(tk):
        return t2cik.get(tk) or t2cik.get(tk.replace("-", ".")) or t2cik.get(tk.replace("-", ""))
    c2t = {}
    for tk in tickers:
        cik = resolve_cik(tk)
        if cik and cik not in c2t:
            c2t[cik] = tk
    print(f"  {len(c2t)}/{len(tickers)} resolved")

    print("\n[3] Price history …")
    price_start = (pd.Timestamp(BACKTEST_START) - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
    prices  = fetch_prices(tickers, price_start, BACKTEST_END)
    tickers = [t for t in tickers if t in prices.columns]
    universe = set(tickers)
    print(f"  {len(tickers)} tickers with price data")

    print("\n[4] Required fundamental periods …")
    rebal_dates = get_rebalance_dates(BACKTEST_START, BACKTEST_END)

    # QI (balance sheet) quarters needed
    needed_qi: set[tuple[int, int]] = set()
    # Annual years needed for flow data
    needed_ann: set[int] = set()

    for rd in rebal_dates:
        yq      = get_available_quarter(rd)
        ann_yr  = get_available_annual_year(rd)
        needed_qi.add(yq)
        needed_qi.add(yq_offset(yq, 1))    # year-ago balance sheet for Piotroski
        needed_ann.add(ann_yr)
        needed_ann.add(ann_yr - 1)          # year-ago flows for Piotroski + EPS surprise
        needed_ann.add(ann_yr - 3)          # 3-year-ago for revenue growth

    needed_qi  = sorted(needed_qi)
    needed_ann = sorted(needed_ann)
    print(f"  {len(needed_qi)} unique QI quarters: {needed_qi[0]} → {needed_qi[-1]}")
    print(f"  {len(needed_ann)} unique annual years: {needed_ann[0]} → {needed_ann[-1]}")

    print(f"\n[5] Fetching EDGAR concepts …")
    print(f"  Balance sheet (QI): {len(CONCEPTS_QI)} concepts × {len(needed_qi)} quarters")
    raw_frames_qi: dict = {}
    for i, (yr, qtr) in enumerate(needed_qi):
        print(f"  [{i+1:2}/{len(needed_qi)}] QI Q{qtr}/{yr}", end=" … ", flush=True)
        raw_frames_qi[(yr, qtr)] = {k: fetch_xbrl_frame(k, yr, qtr) for k in CONCEPTS_QI}
        print(f"{len(raw_frames_qi[(yr, qtr)]['assets'])} co.")

    print(f"  Annual flows: {len(CONCEPTS_ANN)} concepts × {len(needed_ann)} years")
    raw_frames_ann: dict = {}
    for i, yr in enumerate(needed_ann):
        print(f"  [{i+1:2}/{len(needed_ann)}] ANN {yr}", end=" … ", flush=True)
        raw_frames_ann[yr] = {k: fetch_xbrl_annual(k, yr) for k in CONCEPTS_ANN}
        print(f"{len(raw_frames_ann[yr]['revenue'])} co.")

    FUND_COLS_QI  = list(CONCEPTS_QI.keys())
    FUND_COLS_ANN = list(CONCEPTS_ANN.keys())
    yago_qi_cols  = ["assets", "ltd", "curr_assets", "curr_liab", "shares"]
    yago_ann_cols = ["net_income", "gross_profit", "revenue"]

    print("\n[6] Building PIT factor tables …")
    all_fund:       dict = {}
    all_fund_yago:  dict = {}
    all_fund_3yago: dict = {}
    all_eps:        dict = {}

    for rd in rebal_dates:
        yq      = get_available_quarter(rd)
        ann_yr  = get_available_annual_year(rd)
        if yq in all_fund:
            continue

        qi_c  = raw_frames_qi.get(yq,              {})
        qi_y  = raw_frames_qi.get(yq_offset(yq,1), {})
        ann_c = raw_frames_ann.get(ann_yr,     {})
        ann_y = raw_frames_ann.get(ann_yr - 1, {})
        ann_3 = raw_frames_ann.get(ann_yr - 3, {})

        all_fund[yq] = build_fundamental_df(
            qi_c, c2t, universe, FUND_COLS_QI,
            ann_frames=ann_c, ann_cols=FUND_COLS_ANN,
        )
        all_fund_yago[yq] = build_fundamental_df(
            qi_y, c2t, universe, yago_qi_cols,
            ann_frames=ann_y, ann_cols=yago_ann_cols,
        )
        all_fund_3yago[yq] = build_fundamental_df(
            {}, c2t, universe, [],
            ann_frames=ann_3, ann_cols=["revenue"],
        )

        # EPS surprise: annual YoY
        def _map_eps(frames_ann):
            s = frames_ann.get("eps", pd.Series(dtype=float)).copy()
            s.index = [c2t.get(c) for c in s.index]
            return s[s.index.notna() & s.index.isin(universe)]

        fc = _map_eps(ann_c)
        fy = _map_eps(ann_y)
        fc, fy = fc.align(fy, join="inner")
        all_eps[yq] = ((fc - fy) / fy.abs().clip(lower=0.01)).clip(-5, 5)

    print("\n[7] Backtest …")
    strategy, benchmark = run_backtest(
        prices, all_fund, all_fund_yago, all_fund_3yago, all_eps,
        BACKTEST_START, BACKTEST_END,
    )

    print("\n[8] Performance:")
    sm = metrics(strategy)
    bm = metrics(benchmark)
    print(f"  {'Metric':<12} {'Strategy':>10} {'SPY':>10}")
    print("  " + "-" * 34)
    for k in sm:
        print(f"  {k:<12} {sm[k]:>10} {bm[k]:>10}")

    plot_results(strategy, benchmark, "/Users/vishalgupta/claude/backtest_results_v3.png")

    print("\n[9] Current screen …")
    end_ts    = pd.Timestamp(BACKTEST_END)
    yq_now    = get_available_quarter(end_ts)
    ann_now   = get_available_annual_year(end_ts)
    fund_now  = all_fund.get(yq_now,      pd.DataFrame())
    f_yago    = all_fund_yago.get(yq_now,  pd.DataFrame())
    f_3yago   = all_fund_3yago.get(yq_now, pd.DataFrame())
    eps_now   = all_eps.get(yq_now,        pd.Series(dtype=float))

    if not fund_now.empty:
        scores = build_composite(tickers, prices, fund_now, f_yago, f_3yago,
                                 eps_now, end_ts)
        n      = max(1, int(len(scores) * TOP_DECILE_PCT))
        print(f"\n  Top {n} stocks (BS Q{yq_now[1]}/{yq_now[0]}, flows {ann_now}, after hard filters):")
        for rank, (tk, sc) in enumerate(scores.head(n).items(), 1):
            print(f"  {rank:3}. {tk:<8} {sc:.3f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
