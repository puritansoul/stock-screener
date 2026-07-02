"""
Live Multi-Factor S&P 500 Monitor & Rebalance Notifier

- Holdings only change at quarterly rebalance (Mar/Jun/Sep/Dec)
- Daily screen detects HIGH-CONVICTION alerts: stocks outside your
  current portfolio that score in the top 5% — highlighted in gold
- Portfolio performance tracked since inception: daily, 1M, 3M, 6M, 1Y, 3Y, 5Y
- Runs at 10am weekdays via cron

Setup (run once):
  python3 live_monitor.py --setup
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ── User config ───────────────────────────────────────────────────────────────
PORTFOLIO_VALUE       = 100_000     # your portfolio size in $
TOP_DECILE_PCT        = 0.10        # top 10% = your holdings
HIGH_CONVICTION_PCT   = 0.05        # top 5% = alert threshold outside rebalance
REBALANCE_MONTHS      = {3, 6, 9, 12}
REBALANCE_WINDOW      = 5           # days into month to show rebalance prompt
FILING_LAG_DAYS       = 75
CRON_HOUR             = 10          # run at 10am

BASE_DIR   = Path(__file__).parent
CACHE_DIR  = BASE_DIR / "edgar_cache"
STATE_FILE = BASE_DIR / "portfolio_state.json"
NAV_FILE   = BASE_DIR / "portfolio_nav.json"
REPORT_DIR = BASE_DIR / "reports"

SEC_DELAY_S  = 0.12
SEC_HEADERS  = {"User-Agent": "QuantResearch screener@research.com"}
WIKI_HEADERS = {"User-Agent": "Mozilla/5.0"}

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

# Hard filter thresholds
F_MKTCAP_MIN      = 1e9
F_ROIC_MIN        = 0.08
F_PIOTROSKI_MIN   = 5
F_DEBT_EBITDA_MAX = 3.5
F_EV_EBITDA_MAX   = 25.0

# Scoring weights
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

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "holdings": [],
        "last_rebalance": None,
        "last_run": None,
        "inception_date": None,
        "prev_prices": {},
    }

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))

def load_nav() -> dict:
    if NAV_FILE.exists():
        return json.loads(NAV_FILE.read_text())
    return {}   # {date_str: nav_value}

def save_nav(nav: dict) -> None:
    NAV_FILE.write_text(json.dumps(nav))

# ── Notifications ─────────────────────────────────────────────────────────────

def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Glass"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass

def open_report(path: str) -> None:
    try:
        subprocess.run(["open", path], check=False)
    except Exception:
        pass

# ── Disk cache ────────────────────────────────────────────────────────────────

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

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────

def get_ticker_cik_map() -> dict[str, str]:
    cached = load_cache("ticker_cik_map")
    if cached:
        return cached
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={**SEC_HEADERS, "Host": "www.sec.gov"}, timeout=30,
    )
    data    = resp.json()
    mapping = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    save_cache("ticker_cik_map", mapping)
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
    q_ends    = pd.date_range("2009-01-01", str(as_of.year + 1), freq="QE")
    available = [(d.year, (d.month - 1) // 3 + 1) for d in q_ends if d <= cutoff]
    return available[-1] if available else (2014, 4)


def get_available_annual_year(as_of: pd.Timestamp, lag: int = FILING_LAG_DAYS) -> int:
    """
    Return the most recent fiscal year whose Dec 31 10-K would be available
    by as_of date given the filing lag. A Dec 31 year-end 10-K is considered
    available 75+ days after Dec 31 of that year.
    """
    cutoff = as_of - pd.Timedelta(days=lag)
    # Dec 31 year-ends only
    year = cutoff.year
    # If we haven't yet passed Dec 31 + lag of the current year, use previous year
    dec31 = pd.Timestamp(f"{year}-12-31")
    if dec31 > cutoff:
        year -= 1
    return max(year, 2009)

# ── Prices ────────────────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    url  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=WIKI_HEADERS, timeout=15).text
    return pd.read_html(StringIO(html))[0]["Symbol"].str.replace(".", "-", regex=False).tolist()


def fetch_prices(tickers: list[str], lookback_months: int = 14) -> pd.DataFrame:
    start = (pd.Timestamp.today() - pd.DateOffset(months=lookback_months)).strftime("%Y-%m-%d")
    raw   = yf.download(tickers, start=start, auto_adjust=True, progress=False, threads=True)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    return close.dropna(axis=1, how="all")

# ── Factor scoring ────────────────────────────────────────────────────────────

def pct_rank(s: pd.Series, higher_is_better: bool = True) -> pd.Series:
    r = s.rank(pct=True, na_option="keep")
    return r if higher_is_better else (1 - r)


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


def _safe(s: pd.Series) -> pd.Series:
    return s.replace([np.inf, -np.inf], np.nan)


def _compute_roic(fund: pd.DataFrame) -> pd.Series:
    ic   = fund.get("equity", pd.Series(dtype=float)).add(
        fund.get("ltd", pd.Series(dtype=float)).fillna(0), fill_value=np.nan)
    ebit = fund.get("ebit", pd.Series(dtype=float))
    ic_safe = ic.where(ic > fund["assets"].clip(lower=1) * 0.05, other=fund["assets"])
    return _safe(ebit / ic_safe.clip(lower=1))


def _compute_fcf_ev(fund: pd.DataFrame, price_snap: pd.Series) -> pd.Series:
    capex   = fund.get("capex", pd.Series(dtype=float)).fillna(0).abs()
    fcf     = fund.get("op_cf", pd.Series(dtype=float)) - capex
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    ev      = mkt_cap + fund["ltd"].fillna(0) - fund["cash"].fillna(0)
    return _safe(fcf / ev.clip(lower=1)).where(ev > 0)


def _compute_ev_ebitda(fund: pd.DataFrame, price_snap: pd.Series) -> pd.Series:
    ebitda  = fund.get("ebit", pd.Series(dtype=float)) + \
              fund.get("dna",  pd.Series(dtype=float)).fillna(0)
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    ev      = mkt_cap + fund["ltd"].fillna(0) - fund["cash"].fillna(0)
    return _safe(ev / ebitda.clip(lower=1)).where(ebitda > 0)


def _compute_debt_ebitda(fund: pd.DataFrame) -> pd.Series:
    ebitda = fund.get("ebit", pd.Series(dtype=float)) + \
             fund.get("dna",  pd.Series(dtype=float)).fillna(0)
    return _safe(fund["ltd"] / ebitda.clip(lower=1)).where(ebitda > 0)


def _compute_piotroski(fund: pd.DataFrame, fund_yago: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    idx = fund.index
    def reindex(df, col):
        if col in df.columns:
            return df[col].reindex(idx)
        return pd.Series(np.nan, index=idx)
    sig = {}
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
    lev   = fund["ltd"] / fund["assets"].clip(lower=1)
    lev_y = reindex(fund_yago, "ltd") / reindex(fund_yago, "assets").clip(lower=1)
    sig["lev_decr"]   = ((lev - lev_y) < 0).astype(float).where((lev - lev_y).notna())
    if "curr_assets" in fund.columns and "curr_liab" in fund.columns:
        cr   = fund["curr_assets"] / fund["curr_liab"].clip(lower=0.01)
        cr_y = reindex(fund_yago, "curr_assets") / reindex(fund_yago, "curr_liab").clip(lower=0.01)
        sig["cr_impr"] = ((cr - cr_y) > 0).astype(float).where((cr - cr_y).notna())
    else:
        sig["cr_impr"] = pd.Series(np.nan, index=idx)
    sh_y = reindex(fund_yago, "shares")
    sig["no_dilution"] = ((fund["shares"] - sh_y) <= sh_y * 0.02).astype(float).where(sh_y.notna())
    if "revenue" in fund.columns:
        gm   = fund["gross_profit"] / fund["revenue"].clip(lower=1)
        gm_y = reindex(fund_yago, "gross_profit") / reindex(fund_yago, "revenue").clip(lower=1)
        sig["gm_impr"] = ((gm - gm_y) > 0).astype(float).where((gm - gm_y).notna())
        at   = fund["revenue"] / fund["assets"].clip(lower=1)
        at_y = reindex(fund_yago, "revenue") / reindex(fund_yago, "assets").clip(lower=1)
        sig["at_impr"] = ((at - at_y) > 0).astype(float).where((at - at_y).notna())
    else:
        sig["gm_impr"] = sig["at_impr"] = pd.Series(np.nan, index=idx)
    df_sig  = pd.DataFrame(sig, index=idx)
    return df_sig.sum(axis=1, min_count=1), df_sig.notna().sum(axis=1)


def _apply_hard_filters(fund, price_snap, p_score, p_n, ev_ebitda, debt_ebitda, roic) -> pd.Series:
    mkt_cap = price_snap.reindex(fund.index) * fund["shares"]
    return (
        (mkt_cap.isna() | (mkt_cap >= F_MKTCAP_MIN)) &
        (roic.isna()      | (roic >= F_ROIC_MIN)) &
        (p_n.isna()       | (p_n < 5) | (p_score >= F_PIOTROSKI_MIN)) &
        (debt_ebitda.isna()| (debt_ebitda <= F_DEBT_EBITDA_MAX)) &
        (ev_ebitda.isna() | (ev_ebitda <= F_EV_EBITDA_MAX))
    )


def score_universe(
    tickers: list[str],
    prices: pd.DataFrame,
    fund: pd.DataFrame,
    fund_yago: pd.DataFrame,
    fund_3yago: pd.DataFrame,
    eps_surprise: pd.Series,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    if fund.empty:
        return pd.DataFrame()

    try:
        price_snap = prices.iloc[-1].reindex(fund.index)
    except (IndexError, KeyError):
        price_snap = pd.Series(np.nan, index=fund.index)

    try:
        p1  = prices.loc[:as_of - pd.DateOffset(months=1)].iloc[-1].reindex(fund.index)
        p12 = prices.loc[:as_of - pd.DateOffset(months=12)].iloc[-1].reindex(fund.index)
        mom = _safe((p1 - p12) / p12)
    except (IndexError, KeyError):
        mom = pd.Series(np.nan, index=fund.index)

    roic       = _compute_roic(fund)
    fcf_ev     = _compute_fcf_ev(fund, price_snap)
    ev_ebitda  = _compute_ev_ebitda(fund, price_snap)
    debt_ebitda= _compute_debt_ebitda(fund)
    accruals   = _safe((fund["net_income"] - fund.get("op_cf", pd.Series(np.nan, index=fund.index)))
                       / fund["assets"].clip(lower=1))
    p_score, p_n = _compute_piotroski(fund, fund_yago)
    if "revenue" in fund.columns and "revenue" in fund_3yago.columns:
        rev3 = fund_3yago["revenue"].reindex(fund.index)
        rev_growth = _safe((fund["revenue"] / rev3.clip(lower=1)).where(rev3 > 0) ** (1/3) - 1)
    else:
        rev_growth = pd.Series(np.nan, index=fund.index)
    eps_surp = eps_surprise.reindex(fund.index)

    mask = _apply_hard_filters(fund, price_snap, p_score, p_n, ev_ebitda, debt_ebitda, roic)

    ranks = pd.DataFrame({
        "r_momentum":    pct_rank(mom,        True),
        "r_roic":        pct_rank(roic,       True),
        "r_fcf_ev":      pct_rank(fcf_ev,     True),
        "r_ev_ebitda":   pct_rank(ev_ebitda,  False),
        "r_piotroski":   pct_rank(p_score,    True),
        "r_accruals":    pct_rank(accruals,   False),
        "r_eps_surprise":pct_rank(eps_surp,   True),
        "r_rev_growth":  pct_rank(rev_growth, True),
    }, index=fund.index)

    # Weighted composite: normalize by the total weight of non-NaN rank columns
    # so that missing data (e.g. EPS annual frame not available) doesn't zero out the score
    rank_weight_map = {
        "r_momentum":     WEIGHTS["momentum"],
        "r_roic":         WEIGHTS["roic"],
        "r_fcf_ev":       WEIGHTS["fcf_ev"],
        "r_ev_ebitda":    WEIGHTS["ev_ebitda"],
        "r_piotroski":    WEIGHTS["piotroski"],
        "r_accruals":     WEIGHTS["accruals"],
        "r_eps_surprise": WEIGHTS["eps_surprise"],
        "r_rev_growth":   WEIGHTS["rev_growth"],
    }
    weighted_sum  = sum(ranks[col].fillna(0) * w for col, w in rank_weight_map.items())
    weight_avail  = sum(ranks[col].notna().astype(float) * w for col, w in rank_weight_map.items())
    composite = _safe(weighted_sum / weight_avail.clip(lower=0.01)).where(weight_avail > 0).where(mask)

    mkt_cap = price_snap * fund["shares"]
    df = ranks.copy()
    df["roic"]        = roic
    df["fcf_ev"]      = fcf_ev
    df["ev_ebitda"]   = ev_ebitda
    df["debt_ebitda"] = debt_ebitda
    df["piotroski"]   = p_score
    df["composite"]   = composite
    df["price"]       = price_snap
    df["market_cap_bn"] = (mkt_cap / 1e9).round(2)
    df = df[df.index.isin(tickers)].dropna(subset=["composite"])
    return df.sort_values("composite", ascending=False)

# ── Position sizing: score × inverse volatility ───────────────────────────────

VOL_LOOKBACK_DAYS = 63   # ~1 quarter of daily returns
MAX_POSITION_PCT  = 0.15  # single position cap: 15%
MIN_POSITION_PCT  = 0.02  # single position floor: 2%

def compute_weights(
    holdings: list[str],
    scores: pd.DataFrame,
    prices: pd.DataFrame,
) -> dict[str, float]:
    """
    Score × inverse-volatility allocation, capped per-position.

    weight_raw[i] = composite_score[i] / vol63[i]
    weight[i]     = weight_raw[i] / sum(weight_raw), then clipped to [MIN, MAX].
    Falls back to equal weight for any ticker missing price history.
    """
    if not holdings:
        return {}

    held    = [t for t in holdings if t in scores.index]
    n       = len(held)
    if n == 0:
        return {t: 1.0 / len(holdings) for t in holdings}

    score_vals = scores.loc[held, "composite"].fillna(scores["composite"].median())

    # 63-day realised volatility (annualised not needed — only relative ordering matters)
    vols = {}
    for t in held:
        if t in prices.columns and len(prices[t].dropna()) >= 20:
            rets      = prices[t].pct_change().dropna().tail(VOL_LOOKBACK_DAYS)
            vols[t]   = rets.std() if len(rets) >= 10 else np.nan
        else:
            vols[t] = np.nan

    vol_series = pd.Series(vols)
    # Fill NaN vols with the median so missing data doesn't zero out the position
    median_vol = vol_series.median()
    if pd.isna(median_vol) or median_vol == 0:
        median_vol = 0.01
    vol_series = vol_series.fillna(median_vol).clip(lower=1e-6)

    # raw weight = score / vol
    raw = score_vals / vol_series.reindex(held)
    raw = raw.clip(lower=0)

    total = raw.sum()
    if total == 0:
        w_norm = pd.Series(1.0 / n, index=held)
    else:
        w_norm = raw / total

    # Clip to [MIN, MAX] and renormalise
    w_clipped = w_norm.clip(lower=MIN_POSITION_PCT, upper=MAX_POSITION_PCT)
    w_final   = w_clipped / w_clipped.sum()

    # Any holding not in scores gets the minimum share
    result = {}
    for t in holdings:
        result[t] = float(w_final.get(t, MIN_POSITION_PCT))
    # Renormalise in case of fallback entries
    total_w = sum(result.values())
    return {t: w / total_w for t, w in result.items()}


# ── Portfolio NAV ─────────────────────────────────────────────────────────────

def update_nav(
    nav_history: dict,
    today: date,
    holdings: list[str],
    weights: dict[str, float],
    prices: pd.DataFrame,
    prev_prices: dict,
) -> tuple[float, float]:
    """
    Compute today's NAV using score×inv-vol weights.
    Returns (today_nav, daily_return_pct).
    """
    today_str = today.isoformat()
    if not holdings or prices.empty:
        last_nav = list(nav_history.values())[-1] if nav_history else 1.0
        nav_history[today_str] = last_nav
        return last_nav, 0.0

    try:
        p_today = prices.iloc[-1]
    except IndexError:
        last_nav = list(nav_history.values())[-1] if nav_history else 1.0
        nav_history[today_str] = last_nav
        return last_nav, 0.0

    last_nav = list(nav_history.values())[-1] if nav_history else 1.0

    # Derive previous close from price history (last 2 rows), falling back to saved prev_prices
    try:
        p_prev_row = prices.iloc[-2] if len(prices) >= 2 else None
    except IndexError:
        p_prev_row = None

    daily_ret = 0.0
    for t in holdings:
        w     = weights.get(t, 1.0 / len(holdings))
        p_now = p_today.get(t)
        # prefer price history row, fall back to saved prev_prices dict
        p_prev = (float(p_prev_row[t]) if p_prev_row is not None and t in p_prev_row.index and pd.notna(p_prev_row[t]) else None) \
                 or prev_prices.get(t)
        if p_now and p_prev and pd.notna(p_now) and float(p_prev) > 0:
            daily_ret += w * (float(p_now) / float(p_prev) - 1)
    new_nav = last_nav * (1 + daily_ret)

    nav_history[today_str] = new_nav
    return new_nav, daily_ret


def compute_period_return(nav_history: dict, today: date, days: int) -> str:
    """Return formatted period return, or '—' if insufficient history."""
    dates  = sorted(nav_history.keys())
    if len(dates) < 2:
        return "—"
    nav_today = nav_history.get(today.isoformat())
    if nav_today is None:
        return "—"

    target_date = today - timedelta(days=days)
    # Find closest date on or after target
    past_dates  = [d for d in dates if d <= target_date.isoformat()]
    if not past_dates:
        return "—"
    nav_past = nav_history[past_dates[-1]]
    if nav_past == 0:
        return "—"
    ret = (nav_today / nav_past) - 1
    color = "#2ca02c" if ret >= 0 else "#d62728"
    sign  = "+" if ret >= 0 else ""
    return f'<span style="color:{color};font-weight:bold">{sign}{ret:.2%}</span>'


def compute_daily_return(nav_history: dict, today: date) -> str:
    dates = sorted(nav_history.keys())
    if len(dates) < 2:
        return "—"
    today_str = today.isoformat()
    if today_str not in nav_history:
        return "—"
    idx      = dates.index(today_str)
    if idx == 0:
        return "—"
    prev_nav = nav_history[dates[idx - 1]]
    today_nav = nav_history[today_str]
    if prev_nav == 0:
        return "—"
    ret   = (today_nav / prev_nav) - 1
    color = "#2ca02c" if ret >= 0 else "#d62728"
    sign  = "+" if ret >= 0 else ""
    return f'<span style="color:{color};font-weight:bold">{sign}{ret:.2%}</span>'

# ── Trade list ────────────────────────────────────────────────────────────────

def compute_trades(
    target: list[str],
    current: list[str],
    weights: dict[str, float],
    scores: pd.DataFrame,
    portfolio_value: float,
) -> dict:
    t_set  = set(target)
    c_set  = set(current)
    trades = {"buy": [], "sell": [], "hold": []}

    for t in sorted(t_set - c_set):
        price   = scores.loc[t, "price"] if t in scores.index else np.nan
        alloc   = weights.get(t, 1.0 / len(target)) * portfolio_value
        shares  = int(alloc / price) if pd.notna(price) and price > 0 else "?"
        trades["buy"].append({
            "ticker": t, "shares": shares,
            "alloc_pct": f"{weights.get(t, 0):.1%}",
            "est_value": f"${alloc:,.0f}",
            "price": f"${price:.2f}" if pd.notna(price) else "?",
        })
    for t in sorted(c_set - t_set):
        trades["sell"].append({"ticker": t})
    for t in sorted(t_set & c_set):
        price = scores.loc[t, "price"] if t in scores.index else np.nan
        trades["hold"].append({
            "ticker": t,
            "alloc_pct": f"{weights.get(t, 0):.1%}",
            "est_value": f"${weights.get(t, 0) * portfolio_value:,.0f}",
            "price": f"${price:.2f}" if pd.notna(price) else "?",
        })
    return trades

# ── Helpers ───────────────────────────────────────────────────────────────────

def _next_rebalance_date(today: date) -> str:
    for offset in range(1, 5):
        m = today.month + offset
        y = today.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        if m in REBALANCE_MONTHS:
            return f"{y}-{m:02d}-01"
    return "?"


def _fmt(val, fmt_str=".2f"):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "—"
    return format(float(val), fmt_str)


def _fmt_price(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "—"
    return f"${float(val):.2f}"


def _score_bar(val, max_val=1.0):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return '<div style="background:#eee;border-radius:3px;width:100px;display:inline-block;vertical-align:middle"><div style="width:0%;height:10px"></div></div>'
    pct   = min(100, int(float(val) / max_val * 100))
    color = "#2ca02c" if pct >= 60 else "#ff7f0e" if pct >= 40 else "#d62728"
    return (f'<div style="background:#eee;border-radius:3px;width:100px;display:inline-block;vertical-align:middle">'
            f'<div style="background:{color};width:{pct}%;height:10px;border-radius:3px"></div></div>')

# ── HTML report ───────────────────────────────────────────────────────────────

def save_html_report(
    today: date,
    scores: pd.DataFrame,
    trades: dict | None,
    yq: tuple[int, int],
    ann_year: int,
    n_target: int,
    is_rebalance: bool,
    holdings: list[str],
    weights: dict[str, float],
    alert_tickers: list[str],
    nav_history: dict,
    inception_date: str | None,
    positions: dict | None = None,
    prev_prices: dict | None = None,
    prices_df: pd.DataFrame | None = None,
) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path      = REPORT_DIR / f"{today.isoformat()}.html"
    next_rb   = _next_rebalance_date(today)
    today_nav = nav_history.get(today.isoformat(), 1.0)
    holdings_set = set(holdings)
    alert_set    = set(alert_tickers)
    positions    = positions or {}
    prev_prices  = prev_prices or {}
    # Derive yesterday's close from price history if available
    if prices_df is not None and len(prices_df) >= 2:
        prev_row = prices_df.iloc[-2]
        for t in holdings_set:
            if t not in prev_prices and t in prev_row.index and pd.notna(prev_row[t]):
                prev_prices[t] = float(prev_row[t])

    # ── Performance cards ──────────────────────────────────────────────────────
    perf_rows = [
        ("Today",        compute_daily_return(nav_history, today)),
        ("1 Month",      compute_period_return(nav_history, today, 30)),
        ("3 Months",     compute_period_return(nav_history, today, 91)),
        ("6 Months",     compute_period_return(nav_history, today, 182)),
        ("1 Year",       compute_period_return(nav_history, today, 365)),
        ("3 Years",      compute_period_return(nav_history, today, 365*3)),
        ("5 Years",      compute_period_return(nav_history, today, 365*5)),
        ("Since Inception", compute_period_return(nav_history, today,
            (today - date.fromisoformat(inception_date)).days if inception_date else 0)),
    ]
    perf_cards = "".join(
        f'<div style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:8px;padding:14px 18px;min-width:110px;text-align:center">'
        f'<div style="font-size:11px;color:#6c757d;margin-bottom:4px">{label}</div>'
        f'<div style="font-size:18px">{val}</div>'
        f'</div>'
        for label, val in perf_rows
    )
    current_value = today_nav * PORTFOLIO_VALUE
    gain_loss     = current_value - PORTFOLIO_VALUE
    gain_loss_pct = (today_nav - 1.0) * 100
    gain_color    = "#2e7d32" if gain_loss >= 0 else "#c62828"
    gain_sign     = "+" if gain_loss >= 0 else ""
    nav_str       = f"${current_value:,.0f}"

    # Portfolio daily % change (raw value for coloring)
    _dates = sorted(nav_history.keys())
    _today_str = today.isoformat()
    _port_day_raw = 0.0
    if _today_str in _dates and _dates.index(_today_str) > 0:
        _prev_date = _dates[_dates.index(_today_str) - 1]
        _prev_nav  = nav_history.get(_prev_date, today_nav)
        if _prev_nav > 0:
            _port_day_raw = (today_nav / _prev_nav) - 1
    port_day_color = "#2e7d32" if _port_day_raw >= 0 else "#c62828"
    port_day_sign  = "+" if _port_day_raw >= 0 else ""
    port_day_d     = _port_day_raw * current_value
    port_day_str   = f'{port_day_sign}${abs(port_day_d):,.0f} ({port_day_sign}{_port_day_raw:.2%})'

    # ── Rebalance section ──────────────────────────────────────────────────────
    trade_html = ""
    if trades and is_rebalance:
        buy_rows  = "".join(
            f'<tr style="background:#e8f5e9"><td>🟢 BUY</td><td><b>{t["ticker"]}</b></td>'
            f'<td style="font-weight:bold">{t["alloc_pct"]}</td>'
            f'<td>{t["shares"]} shares</td><td>{t["est_value"]}</td><td>@ {t["price"]}</td></tr>'
            for t in trades["buy"]
        )
        sell_rows = "".join(
            f'<tr style="background:#ffebee"><td>🔴 SELL</td><td><b>{t["ticker"]}</b></td>'
            f'<td colspan="4">Exit full position</td></tr>'
            for t in trades["sell"]
        )
        hold_rows = "".join(
            f'<tr style="background:#f5f5f5"><td>⚪ HOLD</td><td><b>{t["ticker"]}</b></td>'
            f'<td style="font-weight:bold">{t["alloc_pct"]}</td>'
            f'<td colspan="1">Resize to {t["est_value"]}</td><td></td><td>@ {t["price"]}</td></tr>'
            for t in trades["hold"]
        )
        trade_html = f"""
        <div style="background:#fff3e0;border:2px solid #e65100;border-radius:8px;padding:20px;margin:20px 0">
          <h2 style="color:#e65100;margin-top:0">⚡ Quarterly Rebalance Required</h2>
          <p>Portfolio value: <b>{nav_str}</b> &nbsp;|&nbsp; {n_target} holdings &nbsp;|&nbsp;
          Allocation: Score × Inverse-Volatility (63-day), capped {MIN_POSITION_PCT:.0%}–{MAX_POSITION_PCT:.0%}</p>
          <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-family:monospace;font-size:13px">
            <tr style="background:#263238;color:white"><th>Action</th><th>Ticker</th><th>Allocation</th><th>Shares</th><th>Value</th><th>Price</th></tr>
            {buy_rows}{sell_rows}{hold_rows}
          </table>
          <p style="color:#777;font-size:12px;margin-bottom:0">⚠️ Share counts are estimates at last close. Use limit orders at or near the open.
          HOLD rows need resizing to match the new allocation — don't just leave existing sizes unchanged.</p>
        </div>"""

    # ── High-conviction alert section ─────────────────────────────────────────
    alert_html = ""
    if alert_tickers and not is_rebalance:
        alert_rows = ""
        for tk in alert_tickers:
            if tk not in scores.index:
                continue
            row  = scores.loc[tk]
            comp = row["composite"]
            alert_rows += f"""
            <tr style="background:#fffde7">
              <td style="font-weight:bold;color:#f57f17">⭐ {tk}</td>
              <td>{_fmt_price(row.get('price'))}</td>
              <td>{_fmt(row.get('market_cap_bn'), '.1f')}B</td>
              <td>{_score_bar(row.get('r_momentum'))} {_fmt(row.get('r_momentum'))}</td>
              <td>{_score_bar(row.get('r_roic'))} {_fmt(row.get('roic'), '.1%')}</td>
              <td>{_score_bar(row.get('r_fcf_ev'))} {_fmt(row.get('fcf_ev'), '.1%')}</td>
              <td>{_score_bar(row.get('r_ev_ebitda'))} {_fmt(row.get('ev_ebitda'), '.1f')}x</td>
              <td>{_score_bar(row.get('r_piotroski'))} {_fmt(row.get('piotroski'), '.0f')}/9</td>
              <td>{_score_bar(row.get('r_accruals'))} {_fmt(row.get('r_accruals'))}</td>
              <td>{_score_bar(row.get('r_eps_surprise'))} {_fmt(row.get('r_eps_surprise'))}</td>
              <td>{_score_bar(comp)} <strong>{_fmt(comp, '.3f')}</strong></td>
            </tr>"""
        if alert_rows:
            alert_html = f"""
            <div style="background:#fffde7;border:2px solid #f9a825;border-radius:8px;padding:20px;margin:20px 0">
              <h2 style="color:#f57f17;margin-top:0">⭐ High-Conviction Picks (Top 5% — Not In Your Portfolio)</h2>
              <p style="color:#555;font-size:13px">These stocks score in the top 5% today but are outside your current quarterly holdings.
              They are <b>not a signal to trade now</b> — they are flagged so you can consider them at your next rebalance.</p>
              <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px">
                <tr style="background:#f57f17;color:white">
                  <th>Ticker</th><th>Price</th><th>Mkt Cap</th>
                  <th>Momentum</th><th>ROIC</th><th>FCF/EV</th><th>EV/EBITDA</th>
                  <th>Piotroski</th><th>Accruals</th><th>EPS Surp</th><th>Score</th>
                </tr>
                {alert_rows}
              </table>
            </div>"""

    # ── Build rows for both tables ─────────────────────────────────────────────
    holdings_rows = ""   # compact table (always visible)
    factor_rows   = ""   # factor-score table (collapsible)

    for rank, (tk, row) in enumerate(scores.head(max(n_target, 50)).iterrows(), 1):
        comp         = row["composite"]
        in_portfolio = tk in holdings_set
        is_alert     = tk in alert_set
        if not in_portfolio and not is_alert and rank > n_target:
            continue

        row_style_held  = 'background:#e8f5e9'
        row_style_alert = 'background:#fffde7'
        row_style = row_style_held if in_portfolio else (row_style_alert if is_alert else '')

        badge = (
            '<span style="background:#388e3c;color:white;border-radius:4px;padding:1px 6px;font-size:11px">HELD</span>' if in_portfolio else
            '<span style="background:#f9a825;color:#333;border-radius:4px;padding:1px 6px;font-size:11px">ALERT</span>' if is_alert else
            ''
        )

        # Position data
        alloc_pct = weights.get(tk, 0.0)
        alloc_str = f"{alloc_pct:.1%}" if in_portfolio else "—"
        pos       = positions.get(tk, {}) if in_portfolio else {}
        qty       = pos.get("shares", 0)
        buy_px    = pos.get("price")
        buy_dt    = pos.get("date", "—")

        # $ invested = shares × buy_price
        cur_px_val = row.get("price")
        if qty and buy_px and buy_px > 0:
            invested  = qty * buy_px
            invested_str = f"${invested:,.0f}"
        else:
            invested_str = "—"

        qty_str  = f"{qty:,}" if qty else "—"
        buy_str  = f"${buy_px:,.2f}" if buy_px else "—"

        # Daily change: (cur - prev) × shares
        prev_px = prev_prices.get(tk)
        if qty and cur_px_val and prev_px and prev_px > 0:
            day_chg_d = (cur_px_val - prev_px) * qty
            day_chg_p = (cur_px_val - prev_px) / prev_px
            day_color = "#2e7d32" if day_chg_d >= 0 else "#c62828"
            day_sign  = "+" if day_chg_d >= 0 else ""
            day_chg_str = (
                f'<span style="color:{day_color};font-weight:bold">'
                f'{day_sign}${abs(day_chg_d):,.0f}</span>'
            )
            day_pct_str = (
                f'<span style="color:{day_color};font-weight:bold">'
                f'{day_sign}{day_chg_p:.2%}</span>'
            )
        else:
            day_chg_str = "—"
            day_pct_str = "—"

        # Total return: (cur - buy) × shares
        if qty and cur_px_val and buy_px and buy_px > 0:
            tot_ret_d = (cur_px_val - buy_px) * qty
            tot_ret_p = (cur_px_val - buy_px) / buy_px
            tot_color = "#2e7d32" if tot_ret_d >= 0 else "#c62828"
            tot_sign  = "+" if tot_ret_d >= 0 else ""
            tot_ret_str = (
                f'<span style="color:{tot_color};font-weight:bold">'
                f'{tot_sign}${abs(tot_ret_d):,.0f} ({tot_sign}{tot_ret_p:.2%})</span>'
            )
        else:
            tot_ret_str = "—"

        # ── Compact holdings row ────────────────────────────────────────────
        holdings_rows += f"""
        <tr style="{row_style}">
          <td style="text-align:center;font-size:12px;color:#666">{rank}</td>
          <td style="font-weight:bold;white-space:nowrap">{tk} {badge}</td>
          <td style="text-align:right">{_fmt_price(row.get('price'))}</td>
          <td style="text-align:right;color:#1a237e;font-weight:bold">{alloc_str}</td>
          <td style="text-align:right">{qty_str}</td>
          <td style="text-align:right">{buy_str}</td>
          <td style="text-align:right;color:#555;font-size:12px">{buy_dt}</td>
          <td style="text-align:right;font-weight:bold">{invested_str}</td>
          <td style="text-align:right">{day_chg_str}</td>
          <td style="text-align:right">{day_pct_str}</td>
          <td style="text-align:right">{tot_ret_str}</td>
          <td style="text-align:right;font-size:12px;color:#555">{_fmt(comp, '.3f')}</td>
        </tr>"""

        # ── Factor scores row ───────────────────────────────────────────────
        factor_rows += f"""
        <tr style="{row_style}">
          <td style="text-align:center;font-size:12px;color:#666">{rank}</td>
          <td style="font-weight:bold;white-space:nowrap">{tk} {badge}</td>
          <td>{_score_bar(row.get('r_momentum'))} {_fmt(row.get('r_momentum'))}</td>
          <td>{_score_bar(row.get('r_roic'))} {_fmt(row.get('roic'), '.1%')}</td>
          <td>{_score_bar(row.get('r_fcf_ev'))} {_fmt(row.get('fcf_ev'), '.1%')}</td>
          <td>{_score_bar(row.get('r_ev_ebitda'))} {_fmt(row.get('ev_ebitda'), '.1f')}x</td>
          <td>{_score_bar(row.get('r_piotroski'))} {_fmt(row.get('piotroski'), '.0f')}/9</td>
          <td>{_score_bar(row.get('r_accruals'))} {_fmt(row.get('r_accruals'))}</td>
          <td>{_score_bar(row.get('r_eps_surprise'))} {_fmt(row.get('r_eps_surprise'))}</td>
          <td>{_score_bar(comp)} <strong>{_fmt(comp, '.3f')}</strong></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Factor Screener — {today.isoformat()}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body   {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
              margin: 0; padding: 16px 20px; color: #222; background:#f0f2f5; }}
    h1     {{ color: #1a237e; margin-bottom:4px; font-size:22px; }}
    h2     {{ color: #1a237e; font-size:16px; margin:0 0 10px 0; }}
    table  {{ border-collapse: collapse; width: 100%; font-size:13px; }}
    th     {{ background: #1a237e; color: white; padding: 8px 10px;
              text-align: left; white-space: nowrap; }}
    td     {{ padding: 6px 10px; border-bottom: 1px solid #eee; white-space: nowrap; }}
    tr:hover td {{ filter: brightness(0.97); }}
    .badge {{ background:#e3f2fd; color:#0d47a1; padding:3px 10px;
              border-radius:12px; font-size:12px; font-weight:bold; }}
    .card-row {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0 18px 0; }}
    .section  {{ background:white; border:1px solid #e0e0e0; border-radius:8px;
                 padding:18px 20px; margin:12px 0; overflow-x:auto; }}
    details summary {{
      cursor: pointer; font-size:16px; font-weight:bold; color:#1a237e;
      padding: 4px 0; user-select:none; list-style:none;
    }}
    details summary::before {{ content: "▶ "; font-size:12px; }}
    details[open] summary::before {{ content: "▼ "; font-size:12px; }}
    details summary::-webkit-details-marker {{ display:none; }}
  </style>
</head>
<body>
  <h1>📊 Multi-Factor S&amp;P 500 Screener</h1>
  <p style="margin:6px 0 12px">
    <span class="badge">{today.isoformat()}</span>&nbsp;
    <span class="badge">BS Q{yq[1]}/{yq[0]} | Flows {ann_year}</span>&nbsp;
    <span class="badge">{len(holdings)} holdings</span>&nbsp;
    <span class="badge">Next rebalance: {next_rb}</span>&nbsp;
    <span class="badge">NAV: {nav_str}</span>
  </p>

  <!-- Performance -->
  <div class="section">
    <h2>Portfolio Performance</h2>
    <p style="color:#666;font-size:12px;margin:-4px 0 14px">
      Inception: {inception_date or today.isoformat()} &nbsp;|&nbsp; Starting value: ${PORTFOLIO_VALUE:,.0f}
    </p>
    <div style="display:flex;align-items:stretch;gap:16px;margin-bottom:20px;flex-wrap:wrap">
      <div style="background:#f0f4ff;border:1px solid #c5cae9;border-radius:10px;padding:16px 24px;min-width:160px">
        <div style="font-size:11px;color:#6c757d;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Current Value</div>
        <div style="font-size:32px;font-weight:bold;color:#1a237e;line-height:1">{nav_str}</div>
      </div>
      <div style="background:#f0f4ff;border:1px solid #c5cae9;border-radius:10px;padding:16px 24px;min-width:160px">
        <div style="font-size:11px;color:#6c757d;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Today</div>
        <div style="font-size:24px;font-weight:bold;color:{port_day_color};line-height:1">{port_day_str}</div>
      </div>
      <div style="background:#f0f4ff;border:1px solid #c5cae9;border-radius:10px;padding:16px 24px;min-width:160px">
        <div style="font-size:11px;color:#6c757d;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Total Return</div>
        <div style="font-size:24px;font-weight:bold;color:{gain_color};line-height:1">
          {gain_sign}${abs(gain_loss):,.0f} <span style="font-size:16px">({gain_sign}{gain_loss_pct:.2f}%)</span>
        </div>
      </div>
    </div>
    <div class="card-row">{perf_cards}</div>
  </div>

  {trade_html}
  {alert_html}

  <!-- Holdings table (always visible) -->
  <div class="section">
    <h2>Holdings &amp; Positions</h2>
    <p style="color:#555;font-size:12px;margin:-4px 0 10px">
      <span style="background:#e8f5e9;padding:2px 8px;border-radius:4px">Green = held</span>&nbsp;
      <span style="background:#fffde7;padding:2px 8px;border-radius:4px">Yellow = high-conviction alert (not held)</span>
    </p>
    <table>
      <thead><tr>
        <th>#</th><th>Ticker</th><th>Price</th>
        <th>Alloc %</th><th>Qty</th><th>Buy Price</th><th>Buy Date</th>
        <th>$ Invested</th><th>Day Δ $</th><th>Day Δ %</th><th>Total Return</th><th>Score</th>
      </tr></thead>
      <tbody>{holdings_rows}</tbody>
    </table>
  </div>

  <!-- Factor scores (collapsible) -->
  <div class="section">
    <details>
      <summary>Factor Scores &amp; Composite — Full Universe Top Ranks</summary>
      <p style="color:#555;font-size:12px;margin:8px 0">
        Hard filters: MktCap≥$1B · ROIC≥8% · Piotroski≥5/9 · Debt/EBITDA≤3.5x · EV/EBITDA≤25x<br>
        Weights: Momentum 20% · ROIC 20% · FCF/EV 15% · EV/EBITDA 10% · Piotroski 10% · Accruals 10% · EPS Surp 10% · Rev 5%
      </p>
      <table>
        <thead><tr>
          <th>#</th><th>Ticker</th>
          <th>Momentum (20%)</th><th>ROIC (20%)</th><th>FCF/EV (15%)</th>
          <th>EV/EBITDA (10%)</th><th>Piotroski (10%)</th>
          <th>Accruals (10%)</th><th>EPS Surp (10%)</th><th>Composite</th>
        </tr></thead>
        <tbody>{factor_rows}</tbody>
      </table>
    </details>
  </div>

  <p style="color:#bbb;font-size:11px;margin-top:16px">
    live_monitor.py &nbsp;|&nbsp; Data: SEC EDGAR XBRL + Yahoo Finance &nbsp;|&nbsp; Not financial advice.
  </p>
</body>
</html>"""

    path.write_text(html)
    return str(path)

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    today = date.today()
    as_of = pd.Timestamp(today)
    state = load_state()
    nav   = load_nav()

    is_rebalance = today.month in REBALANCE_MONTHS and today.day <= REBALANCE_WINDOW

    print(f"\n{'='*60}")
    print(f"  Factor Screener — {today.isoformat()}")
    print(f"  {'** REBALANCE WINDOW **' if is_rebalance else 'Daily screen'}")
    print(f"{'='*60}\n")

    # Set inception date on first run
    if not state.get("inception_date"):
        state["inception_date"] = today.isoformat()

    # 1. Universe
    print("[1] S&P 500 tickers …")
    tickers = get_sp500_tickers()

    # 2. CIK map
    print("[2] CIK map …")
    t2cik = get_ticker_cik_map()
    def resolve_cik(tk):
        return t2cik.get(tk) or t2cik.get(tk.replace("-", ".")) or t2cik.get(tk.replace("-", ""))
    c2t = {}
    for tk in tickers:
        cik = resolve_cik(tk)
        if cik and cik not in c2t:
            c2t[cik] = tk

    # 3. Prices
    print("[3] Prices (14-month lookback) …")
    prices  = fetch_prices(tickers)
    tickers = [t for t in tickers if t in prices.columns]
    universe = set(tickers)

    # 4. Fundamentals — quarterly-instant (balance sheet) + annual (flow items)
    yq        = get_available_quarter(as_of)
    ann_year  = get_available_annual_year(as_of)
    print(f"[4] SEC EDGAR balance sheet Q{yq[1]}/{yq[0]}, annual flows {ann_year} + year-ago + 3yr-ago …")

    # Balance sheet: quarterly instant frames (good coverage already)
    qi_cols  = list(CONCEPTS_QI.keys())
    ann_cols = list(CONCEPTS_ANN.keys())

    def fetch_qi(yq_key):
        return {k: fetch_xbrl_frame(k, yq_key[0], yq_key[1]) for k in CONCEPTS_QI}

    def fetch_ann(year):
        return {k: fetch_xbrl_annual(k, year) for k in CONCEPTS_ANN}

    frames_qi_c  = fetch_qi(yq)
    frames_ann_c = fetch_ann(ann_year)
    frames_ann_y = fetch_ann(ann_year - 1)
    frames_ann_3y= fetch_ann(ann_year - 3)
    frames_qi_y  = fetch_qi((yq[0] - 1, yq[1]))   # year-ago balance sheet for Piotroski

    # Combined current fund DataFrame: balance sheet from QI, flows from annual
    FUND_COLS_QI  = qi_cols
    FUND_COLS_ANN = ann_cols
    fund = build_fundamental_df(
        frames_qi_c, c2t, universe, FUND_COLS_QI,
        ann_frames=frames_ann_c, ann_cols=FUND_COLS_ANN,
    )

    # Year-ago: balance sheet from year-ago QI, flows from (ann_year - 1)
    yago_qi_cols  = ["assets", "ltd", "curr_assets", "curr_liab", "shares"]
    yago_ann_cols = ["net_income", "gross_profit", "revenue"]
    fund_yago = build_fundamental_df(
        frames_qi_y, c2t, universe, yago_qi_cols,
        ann_frames=frames_ann_y, ann_cols=yago_ann_cols,
    )

    # 3-year-ago revenue (annual)
    fund_3yago = build_fundamental_df(
        {}, c2t, universe, [],
        ann_frames=frames_ann_3y, ann_cols=["revenue"],
    )

    # EPS surprise: annual eps current vs year-ago
    def _map_eps(frames_ann):
        s = frames_ann.get("eps", pd.Series(dtype=float)).copy()
        s.index = [c2t.get(c) for c in s.index]
        return s[s.index.notna() & s.index.isin(universe)]

    eps_c = _map_eps(frames_ann_c)
    eps_y = _map_eps(frames_ann_y)
    eps_c, eps_y = eps_c.align(eps_y, join="inner")
    surprise = ((eps_c - eps_y) / eps_y.abs().clip(lower=0.01)).clip(-5, 5)

    # 5. Score (with hard filters)
    print("[5] Scoring (ROIC, FCF/EV, Piotroski, hard filters) …")
    scores = score_universe(tickers, prices, fund, fund_yago, fund_3yago, surprise, as_of)

    if len(scores) < 30:
        # Annual data too sparse for current year — step back one year
        ann_prev  = ann_year - 1
        yq_prev   = (yq[0] - 1, yq[1])
        print(f"  {ann_year} annual too sparse ({len(scores)} passed) — falling back to {ann_prev}")
        frames_ann_cp = fetch_ann(ann_prev)
        frames_ann_yp = fetch_ann(ann_prev - 1)
        frames_ann_3p = fetch_ann(ann_prev - 3)
        frames_qi_cp  = fetch_qi(yq_prev)
        frames_qi_yp  = fetch_qi((yq_prev[0] - 1, yq_prev[1]))
        fund_fb = build_fundamental_df(
            frames_qi_cp, c2t, universe, FUND_COLS_QI,
            ann_frames=frames_ann_cp, ann_cols=FUND_COLS_ANN,
        )
        fund_yago_fb = build_fundamental_df(
            frames_qi_yp, c2t, universe, yago_qi_cols,
            ann_frames=frames_ann_yp, ann_cols=yago_ann_cols,
        )
        fund_3y_fb = build_fundamental_df(
            {}, c2t, universe, [],
            ann_frames=frames_ann_3p, ann_cols=["revenue"],
        )
        eps_fc = _map_eps(frames_ann_cp)
        eps_fy = _map_eps(frames_ann_yp)
        eps_fc, eps_fy = eps_fc.align(eps_fy, join="inner")
        surprise_fb = ((eps_fc - eps_fy) / eps_fy.abs().clip(lower=0.01)).clip(-5, 5)
        scores = score_universe(tickers, prices, fund_fb, fund_yago_fb, fund_3y_fb, surprise_fb, as_of)
        yq = yq_prev
        ann_year = ann_prev

    n_target = max(1, int(len(scores) * TOP_DECILE_PCT))
    n_alert  = max(1, int(len(scores) * HIGH_CONVICTION_PCT))
    target   = scores.head(n_target).index.tolist()
    top5pct  = set(scores.head(n_alert).index.tolist())

    # 6. Update holdings ONLY on rebalance (or first run when state is empty)
    trades  = None
    current = state.get("holdings", [])
    first_run = len(current) == 0

    if is_rebalance or first_run:
        if target:  # only update if we actually scored something
            target_weights = compute_weights(target, scores, prices)
            trades = compute_trades(target, current, target_weights, scores, PORTFOLIO_VALUE) if not first_run else None
            state["holdings"]       = target
            state["last_rebalance"] = today.isoformat()
            current                 = target
            # Record purchase price, date, shares for each new/changed holding
            positions = dict(state.get("positions", {}))
            last_prices = prices.iloc[-1] if len(prices) else pd.Series(dtype=float)
            for tk in target:
                px = float(last_prices[tk]) if tk in last_prices.index and pd.notna(last_prices[tk]) else None
                w  = target_weights.get(tk, 0.0)
                shares = int(PORTFOLIO_VALUE * w / px) if px and px > 0 else 0
                # Only overwrite if ticker is new or being resized at rebalance
                if tk not in positions or is_rebalance:
                    positions[tk] = {
                        "price":  round(px, 4) if px else None,
                        "date":   today.isoformat(),
                        "shares": shares,
                    }
            # Remove positions that were sold (not in new target)
            target_set = set(target)
            positions  = {k: v for k, v in positions.items() if k in target_set}
            state["positions"] = positions

    # On non-rebalance days keep prior holdings; fall back to target if prior is empty
    holdings = current if current else target

    # 6b. Compute score × inverse-vol weights for current holdings
    print("[6b] Computing score × inverse-vol weights …")
    weights = compute_weights(holdings, scores, prices)
    state["weights"] = weights  # persist so other tools can read

    # Backfill positions for any holding that has no record yet (e.g. migrated state)
    positions_map = state.get("positions", {})
    last_prices   = prices.iloc[-1] if len(prices) else pd.Series(dtype=float)
    for tk in holdings:
        if tk not in positions_map:
            px = float(last_prices[tk]) if tk in last_prices.index and pd.notna(last_prices[tk]) else None
            w  = weights.get(tk, 0.0)
            shares = int(PORTFOLIO_VALUE * w / px) if px and px > 0 else 0
            positions_map[tk] = {
                "price":  round(px, 4) if px else None,
                "date":   state.get("last_rebalance", today.isoformat()),
                "shares": shares,
            }
    state["positions"] = positions_map

    # 7. High-conviction alerts: top 5% NOT in current holdings
    alert_tickers = [t for t in top5pct if t not in set(holdings)]

    # 8. Update NAV
    print("[6] Updating portfolio NAV …")
    prev_prices = state.get("prev_prices", {})
    today_nav, daily_ret = update_nav(nav, today, holdings, weights, prices, prev_prices)
    # Save today's prices for tomorrow
    try:
        state["prev_prices"] = {t: float(prices.iloc[-1][t])
                                 for t in holdings if t in prices.columns and pd.notna(prices.iloc[-1][t])}
    except Exception:
        pass
    save_nav(nav)

    state["last_run"]      = today.isoformat()
    state["yq"]            = list(yq)
    state["ann_year"]      = ann_year
    state["n_target"]      = n_target
    state["alert_tickers"] = alert_tickers
    # Save scores so prices-only refresh can reload them without EDGAR
    state["scores"] = scores.where(scores.notna(), other=None).to_dict(orient="index")
    save_state(state)

    # 9. Report
    print("[7] Saving HTML report …")
    report_path = save_html_report(
        today=today,
        scores=scores,
        trades=trades,
        yq=yq,
        ann_year=ann_year,
        n_target=n_target,
        is_rebalance=is_rebalance,
        holdings=holdings,
        alert_tickers=alert_tickers,
        nav_history=nav,
        inception_date=state.get("inception_date"),
        weights=weights,
        positions=state.get("positions", {}),
        prev_prices=prev_prices,
        prices_df=prices,
    )

    # 10. Console summary
    print(f"\n  NAV: ${today_nav * PORTFOLIO_VALUE:,.0f}  (daily: {daily_ret:+.2%})")
    print(f"  Holdings: {len(holdings)}  |  High-conviction alerts: {len(alert_tickers)}")
    if alert_tickers:
        print(f"  Alerts: {', '.join(alert_tickers)}")

    print(f"\n  Top 15:")
    print(f"  {'#':>3}  {'Ticker':<8}  {'Score':>6}  {'Alloc':>6}  {'Price':>8}  {'Status'}")
    print("  " + "-"*56)
    for rank, (tk, row) in enumerate(scores.head(15).iterrows(), 1):
        status = "HELD   " if tk in set(holdings) else ("⭐ ALERT" if tk in top5pct else "       ")
        alloc  = weights.get(tk, 0.0)
        alloc_str = f"{alloc:.1%}" if alloc > 0 else "  —  "
        print(f"  {rank:>3}  {tk:<8}  {row['composite']:>6.3f}  {alloc_str:>6}  {_fmt_price(row.get('price')):>8}  {status}")

    if is_rebalance and trades:
        print(f"\n  *** REBALANCE: BUY {len(trades['buy'])}, SELL {len(trades['sell'])}, HOLD {len(trades['hold'])} ***")
        notify("⚡ Rebalance Due",
               f"Buy {len(trades['buy'])}, Sell {len(trades['sell'])}, Hold {len(trades['hold'])}")
    elif alert_tickers:
        notify("⭐ High-Conviction Alert",
               f"{len(alert_tickers)} new picks outside portfolio: {', '.join(alert_tickers[:3])}")
    else:
        notify("Factor Screener ✓",
               f"Daily: {daily_ret:+.2%} | Next rebalance: {_next_rebalance_date(today)}")

    print(f"\n  Report: {report_path}")
    open_report(report_path)
    print("\nDone.\n")

# ── Setup ─────────────────────────────────────────────────────────────────────

def setup():
    print(f"\nSetting up daily cron job ({CRON_HOUR}am weekdays) …\n")
    python   = sys.executable
    script   = Path(__file__).resolve()
    log      = BASE_DIR / "monitor.log"
    cron_cmd = f"0 {CRON_HOUR} * * 1-5 {python} {script} >> {log} 2>&1"

    result   = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    # Remove any old version of this script's cron line before re-adding
    filtered = "\n".join(
        line for line in existing.splitlines()
        if str(script) not in line
    ).strip()
    new_crontab = (filtered + "\n" + cron_cmd + "\n").lstrip()

    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True)
    if proc.returncode == 0:
        print(f"  ✓ Cron set to {CRON_HOUR}am weekdays:\n    {cron_cmd}\n")
    else:
        print(f"  Add manually via crontab -e:\n\n    {cron_cmd}\n")

    for d in [CACHE_DIR, REPORT_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print(f"  Cache  : {CACHE_DIR}")
    print(f"  Reports: {REPORT_DIR}")
    print(f"  Log    : {log}\n")

# ── Prices-only refresh (fast, no EDGAR) ─────────────────────────────────────

def run_prices_only():
    """
    Reload saved scores + state, fetch current prices only (~30s),
    rebuild the HTML report. Used for intraday dashboard refreshes.
    """
    from datetime import datetime
    now   = datetime.utcnow()
    today = date.today()
    state = load_state()
    nav   = load_nav()

    scores_raw = state.get("scores")
    holdings   = state.get("holdings", [])
    weights    = state.get("weights", {})
    positions  = state.get("positions", {})

    if not scores_raw or not holdings:
        print("No saved scores found — run the full screener first.")
        return

    scores = pd.DataFrame(scores_raw).T
    scores.index.name = "ticker"
    # restore numeric columns
    for col in scores.columns:
        scores[col] = pd.to_numeric(scores[col], errors="ignore")

    # Fetch current prices for holdings + top ranked tickers
    tickers_needed = list(set(holdings) | set(scores.head(50).index.tolist()))
    print(f"Fetching live prices for {len(tickers_needed)} tickers …")
    prices = fetch_prices(tickers_needed, lookback_months=4)

    # Update price column in scores
    if len(prices):
        last = prices.iloc[-1]
        scores["price"] = scores.index.map(lambda t: last.get(t, scores.at[t, "price"] if t in scores.index else None))

    # Update NAV
    prev_prices = state.get("prev_prices", {})
    today_nav, daily_ret = update_nav(nav, today, holdings, weights, prices, prev_prices)
    save_nav(nav)

    # Rebuild report
    yq       = tuple(state.get("yq", [today.year, 1]))
    ann_year = state.get("ann_year", today.year - 1)
    n_target = state.get("n_target", len(holdings))
    alert_tickers = state.get("alert_tickers", [])
    inception_date = state.get("inception_date")

    report_path = save_html_report(
        today=today,
        scores=scores,
        trades=None,
        yq=yq,
        ann_year=ann_year,
        n_target=n_target,
        is_rebalance=False,
        holdings=holdings,
        alert_tickers=alert_tickers,
        nav_history=nav,
        inception_date=inception_date,
        weights=weights,
        positions=positions,
        prev_prices=prev_prices,
        prices_df=prices,
    )
    print(f"Prices-only refresh done — {now.strftime('%H:%M UTC')} → {report_path}")


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup",        action="store_true")
    parser.add_argument("--prices-only",  action="store_true",
                        help="Fast refresh: reload saved scores, fetch live prices only")
    args = parser.parse_args()
    if args.setup:
        setup()
    elif args.prices_only:
        run_prices_only()
    else:
        run()
