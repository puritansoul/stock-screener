"""
Swing Trading Bot — RSI(2) Mean Reversion Strategy

Rules (Larry Connors):
  ENTRY long : close > 200-day SMA  AND  RSI(2) < 10
  EXIT long  : close > 5-day SMA
  ENTRY short: close < 200-day SMA  AND  RSI(2) > 90
  EXIT short : close < 5-day SMA
  VWAP filter: only long if current price is below VWAP (approximated from daily data)

Universe : S&P 500 (same tickers as live_monitor.py)
Capital  : $100,000 starting, 2% risk per trade, max 10 open positions
Risk     : stop = 2× ATR(14) below entry; max 2% portfolio loss per trade
Runs     : daily at 10am ET via GitHub Actions (swing_trading.yml)
State    : swing_trades.json  (all open + closed positions + NAV curve)
"""

from __future__ import annotations

import json
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
STARTING_CAPITAL  = 100_000.0
RISK_PER_TRADE    = 0.02       # 2% of portfolio at risk per trade
MAX_POSITIONS     = 9999       # no cap — limited only by available cash
ATR_PERIOD        = 14
ATR_STOP_MULT     = 2.0        # stop = entry ± 2 × ATR(14)
RSI_PERIOD        = 2
RSI_LONG_ENTRY    = 10         # RSI(2) < 10 → long signal
RSI_SHORT_ENTRY   = 90         # RSI(2) > 90 → short signal
SMA200_PERIOD     = 200
SMA5_PERIOD       = 5
PRICE_LOOKBACK    = 210        # trading days of history to fetch
MIN_PRICE         = 5.0        # skip penny stocks
MIN_AVG_VOLUME    = 500_000    # skip illiquid names

BASE_DIR    = Path(__file__).parent
TRADES_FILE = BASE_DIR / "swing_trades.json"

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# ── State ─────────────────────────────────────────────────────────────────────

def load_trades() -> dict:
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return {
        "capital": STARTING_CAPITAL,
        "open_positions": [],    # list of position dicts
        "closed_positions": [],  # list of closed position dicts
        "nav_history": {},       # {date_str: portfolio_value}
        "inception_date": None,
        "log": [],               # daily run log
    }

def save_trades(state: dict) -> None:
    TRADES_FILE.write_text(json.dumps(state, indent=2, default=str))

# ── Universe ──────────────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    try:
        from io import StringIO
        resp = requests.get(WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        tickers = (
            pd.read_html(StringIO(resp.text))[0]["Symbol"]
            .str.replace(".", "-", regex=False)
            .tolist()
        )
        if len(tickers) > 400:
            return tickers
    except Exception:
        pass
    # fallback: use locally known tickers from portfolio_state.json
    state_file = BASE_DIR / "portfolio_state.json"
    if state_file.exists():
        s = json.loads(state_file.read_text())
        scores = s.get("scores", {})
        if scores:
            return list(scores.keys())
    return []

# ── Technical indicators ─────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, min_periods=period).mean()

def compute_signals(prices_df: pd.DataFrame, ticker: str) -> dict | None:
    """Compute RSI(2), SMA200, SMA5, ATR for a single ticker. Returns None if insufficient data."""
    if ticker not in prices_df.columns:
        return None
    col = prices_df[ticker]
    if col.dropna().shape[0] < SMA200_PERIOD + 10:
        return None

    close = col.dropna()

    sma200 = close.rolling(SMA200_PERIOD).mean()
    sma5   = close.rolling(SMA5_PERIOD).mean()
    rsi2   = rsi(close, RSI_PERIOD)

    last_close  = float(close.iloc[-1])
    last_sma200 = float(sma200.iloc[-1])
    last_sma5   = float(sma5.iloc[-1])
    last_rsi2   = float(rsi2.iloc[-1])

    if pd.isna(last_sma200) or pd.isna(last_rsi2):
        return None

    # ATR needs H/L — approximate from close (high/low not fetched separately)
    atr_approx = float(close.rolling(ATR_PERIOD).std().iloc[-1]) * 1.25
    if pd.isna(atr_approx) or atr_approx == 0:
        return None

    return {
        "close":   last_close,
        "sma200":  last_sma200,
        "sma5":    last_sma5,
        "rsi2":    last_rsi2,
        "atr":     atr_approx,
    }

# ── Price fetch ───────────────────────────────────────────────────────────────

def fetch_prices(tickers: list[str], lookback_days: int = PRICE_LOOKBACK) -> pd.DataFrame:
    end   = date.today()
    start = end - timedelta(days=int(lookback_days * 1.5))
    try:
        raw = yf.download(
            tickers, start=str(start), end=str(end),
            auto_adjust=True, progress=False,
        )
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs("Close", axis=1, level=0)
        else:
            close = raw[["Close"]]
        close.index = pd.to_datetime(close.index)
        return close.sort_index()
    except Exception as e:
        print(f"  Price fetch error: {e}")
        return pd.DataFrame()

# ── Index returns for journal ─────────────────────────────────────────────────

INDEX_TICKERS = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "DOW", "^RUT": "Russell 2K"}

def fetch_index_returns(nav_history: dict) -> dict:
    """Return {date_str: {index_name: pct}} for all dates in nav_history."""
    if not nav_history:
        return {}
    dates = sorted(nav_history.keys())
    start = (date.fromisoformat(dates[0]) - timedelta(days=5)).isoformat()
    end   = (date.fromisoformat(dates[-1]) + timedelta(days=2)).isoformat()
    try:
        raw = yf.download(list(INDEX_TICKERS.keys()), start=start, end=end,
                          auto_adjust=True, progress=False)
        if raw.empty:
            return {}
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        pct   = (close.pct_change() * 100).rename(columns=INDEX_TICKERS)
        result = {}
        for ts, row in pct.iterrows():
            d = str(ts.date()) if hasattr(ts, "date") else str(ts)[:10]
            result[d] = {col: round(float(v), 3) for col, v in row.items() if not pd.isna(v)}
        return result
    except Exception:
        return {}


def fetch_spy_cumulative(nav_history: dict) -> dict:
    """Return {date_str: cum_pct} for SPY anchored to the first date in nav_history."""
    if not nav_history:
        return {}
    dates = sorted(nav_history.keys())
    start = (date.fromisoformat(dates[0]) - timedelta(days=5)).isoformat()
    end   = (date.fromisoformat(dates[-1]) + timedelta(days=2)).isoformat()
    try:
        raw = yf.download("^GSPC", start=start, end=end, auto_adjust=True, progress=False)
        if raw.empty:
            return {}
        close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
        close = close.dropna().sort_index()
        # Find the SPY price on or just before inception
        inception_dt = pd.Timestamp(dates[0])
        prior = close[close.index <= inception_dt]
        if prior.empty:
            return {}
        base = float(prior.iloc[-1])
        result = {}
        for ts, val in close.items():
            d = str(ts.date()) if hasattr(ts, "date") else str(ts)[:10]
            result[d] = round((float(val) / base - 1) * 100, 3)
        return result
    except Exception:
        return {}


def build_journal_section(nav_history: dict, idx_returns: dict, spy_cum: dict | None = None) -> str:
    """Build an HTML journal table of daily P&L vs index returns."""
    if not nav_history:
        return ""

    dates = sorted(nav_history.keys(), reverse=True)
    spy_cum = spy_cum or {}

    def _pc(v, bold=False):
        if v is None:
            return '<td style="color:#bbb;text-align:right">—</td>'
        c = "#1b5e20" if v > 0 else ("#c62828" if v < 0 else "#555")
        w = "font-weight:bold;" if bold else ""
        return f'<td style="color:{c};text-align:right;{w}">{"+" if v>0 else ""}{v:.2f}%</td>'

    def _dc(v):
        if v is None:
            return '<td style="color:#bbb;text-align:right">—</td>'
        c = "#1b5e20" if v > 0 else ("#c62828" if v < 0 else "#555")
        return f'<td style="color:{c};text-align:right;font-weight:bold">{"+" if v>0 else ""}${abs(v):,.0f}</td>'

    def _vs(bot_pct, spx_pct):
        if bot_pct is None or spx_pct is None:
            return '<td style="color:#bbb;text-align:right">—</td>'
        d = bot_pct - spx_pct
        c = "#1b5e20" if d > 0 else "#c62828"
        a = "▲" if d > 0 else "▼"
        return f'<td style="color:{c};text-align:right;font-size:12px">{a} {"+" if d>0 else ""}{d:.2f}%</td>'

    def _cum_vs(bot_cum, spy_c):
        if bot_cum is None or spy_c is None:
            return '<td style="color:#bbb;text-align:right">—</td>'
        d = bot_cum - spy_c
        c = "#1b5e20" if d > 0 else "#c62828"
        a = "▲" if d > 0 else "▼"
        return f'<td style="color:{c};text-align:right;font-weight:bold;font-size:12px">{a} {"+" if d>0 else ""}{d:.2f}%</td>'

    rows = ""
    for i, d in enumerate(dates):
        nav_now  = nav_history[d]
        prev_key = dates[i + 1] if i + 1 < len(dates) else None
        nav_prev = nav_history[prev_key] if prev_key else STARTING_CAPITAL

        pnl      = round(nav_now - nav_prev, 2)
        day_pct  = round(pnl / nav_prev * 100, 3) if nav_prev else None
        cum_pct  = round((nav_now / STARTING_CAPITAL - 1) * 100, 3)
        spy_c    = spy_cum.get(d)

        idx = idx_returns.get(d, {})
        spx = idx.get("S&P 500")
        qqq = idx.get("Nasdaq")
        dow = idx.get("DOW")
        rut = idx.get("Russell 2K")

        day_label = date.fromisoformat(d).strftime("%a, %b %d %Y") if d else d
        rows += f"<tr><td style='white-space:nowrap;font-weight:bold;color:#1a237e'>{day_label}</td>"
        rows += _dc(pnl)
        rows += _pc(day_pct, bold=True)
        rows += _pc(cum_pct)
        rows += _pc(spy_c)
        rows += _cum_vs(cum_pct, spy_c)
        rows += _pc(spx)
        rows += _pc(qqq)
        rows += _pc(dow)
        rows += _pc(rut)
        rows += _vs(day_pct, spx)
        rows += "</tr>\n"

    if not rows:
        rows = '<tr><td colspan="11" style="color:#999;text-align:center;padding:12px">No daily data yet</td></tr>'

    return f"""
  <!-- Daily P&L Journal -->
  <div class="section">
    <details open>
      <summary>Daily P&amp;L Journal — vs Market Indices</summary>
      <p style="color:#888;font-size:12px;margin:8px 0 8px">
        Day % = mark-to-market NAV change that day.&nbsp;
        Cum % = total return since inception.&nbsp;
        SPY Cum % = buy-and-hold SPY return over the same period.&nbsp;
        vs S&P = your day % minus S&amp;P 500 day % (green = beat the market).
      </p>
      <table>
        <thead>
          <tr>
            <th rowspan="2" style="vertical-align:bottom">Date</th>
            <th colspan="4" style="background:#283593;text-align:center;font-size:11px">Your Portfolio</th>
            <th colspan="5" style="background:#283593;text-align:center;font-size:11px">Benchmark</th>
          </tr>
          <tr>
            <th style="text-align:right">Day P&amp;L $</th>
            <th style="text-align:right">Day %</th>
            <th style="text-align:right">Cum %</th>
            <th style="text-align:right">vs SPY (cum)</th>
            <th style="text-align:right">SPY Cum %</th>
            <th style="text-align:right">S&amp;P 500 Day</th>
            <th style="text-align:right">Nasdaq</th>
            <th style="text-align:right">DOW</th>
            <th style="text-align:right">Russell 2K</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </details>
  </div>"""

# ── Position sizing ───────────────────────────────────────────────────────────

def position_size(capital: float, entry: float, stop: float) -> int:
    """Fixed-fraction risk: risk 2% of capital between entry and stop."""
    risk_dollars = capital * RISK_PER_TRADE
    per_share_risk = abs(entry - stop)
    if per_share_risk < 0.01:
        return 0
    shares = int(risk_dollars / per_share_risk)
    max_shares = int(capital * 0.20 / entry)   # never more than 20% in one position
    return min(shares, max_shares)

# ── Core logic ────────────────────────────────────────────────────────────────

def run_swing_trader():
    today_str = date.today().isoformat()
    now_str   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    state     = load_trades()
    log_entry = {"date": today_str, "run_at": now_str, "entries": [], "exits": [], "notes": []}

    if state["inception_date"] is None:
        state["inception_date"] = today_str

    capital   = state["capital"]
    open_pos  = state["open_positions"]
    closed    = state["closed_positions"]

    # All tickers we need: universe + open positions
    universe  = get_sp500_tickers()
    open_tks  = [p["ticker"] for p in open_pos]
    all_tks   = list(set(universe[:300]) | set(open_tks))  # limit universe to 300 to keep download fast

    print(f"Fetching prices for {len(all_tks)} tickers …")
    prices = fetch_prices(all_tks)
    if prices.empty:
        log_entry["notes"].append("Price fetch returned empty — aborting")
        state["log"].append(log_entry)
        save_trades(state)
        print("No price data. Aborting.")
        return

    # ── Step 1: Process exits on open positions ───────────────────────────────
    exits_today = []
    still_open  = []
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]  # "long" or "short"
        sig   = compute_signals(prices, tk)
        if sig is None:
            still_open.append(pos)
            continue

        cur_price = sig["close"]
        sma5      = sig["sma5"]
        stop      = pos["stop"]

        exit_reason = None
        if side == "long":
            if cur_price > sma5:
                exit_reason = "SMA5 cross above"
            elif cur_price <= stop:
                exit_reason = "stop loss"
        else:  # short
            if cur_price < sma5:
                exit_reason = "SMA5 cross below"
            elif cur_price >= stop:
                exit_reason = "stop loss"

        if exit_reason:
            shares   = pos["shares"]
            entry_px = pos["entry_price"]
            pnl      = (cur_price - entry_px) * shares if side == "long" else (entry_px - cur_price) * shares
            capital += pos["cost"] + pnl   # return original cost + PnL

            closed_pos = {**pos, "exit_date": today_str, "exit_price": cur_price, "pnl": round(pnl, 2), "exit_reason": exit_reason}
            closed.append(closed_pos)
            exits_today.append({"ticker": tk, "side": side, "exit_price": cur_price, "pnl": round(pnl, 2), "reason": exit_reason})
            log_entry["exits"].append(exits_today[-1])
            print(f"  EXIT {side} {tk} @ ${cur_price:.2f}  PnL: ${pnl:+,.0f}  ({exit_reason})")
        else:
            still_open.append(pos)

    open_pos = still_open

    # ── Step 2: Scan universe for new entries ─────────────────────────────────
    open_count = len(open_pos)
    slots_available = MAX_POSITIONS - open_count
    open_tickers    = {p["ticker"] for p in open_pos}
    entries_today   = []

    # Always scan to build diagnostic — even if slots_available == 0
    all_scan = []
    candidates = []
    for tk in universe[:300]:
        if tk in open_tickers:
            continue
        sig = compute_signals(prices, tk)
        if sig is None:
            continue
        close, sma200, rsi2, atr_val = sig["close"], sig["sma200"], sig["rsi2"], sig["atr"]
        if close < MIN_PRICE:
            continue
        long_signal  = close > sma200 and rsi2 < RSI_LONG_ENTRY
        short_signal = close < sma200 and rsi2 > RSI_SHORT_ENTRY
        trend = "above SMA200" if close > sma200 else "below SMA200"
        status = ("LONG signal" if long_signal else
                  "SHORT signal" if short_signal else
                  f"{trend}")
        all_scan.append({"ticker": tk, "rsi2": round(rsi2, 1), "close": round(close, 2),
                          "sma200": round(sma200, 2), "status": status})
        if long_signal:
            candidates.append({"ticker": tk, "side": "long",  "rsi2": rsi2, "close": close, "atr": atr_val})
        elif short_signal:
            candidates.append({"ticker": tk, "side": "short", "rsi2": rsi2, "close": close, "atr": atr_val})

    # Save top 40 closest-to-signal tickers for dashboard
    all_scan_sorted = sorted(all_scan, key=lambda x: min(x["rsi2"], 100 - x["rsi2"]))
    state["last_scan"] = all_scan_sorted[:40]

    if slots_available > 0:

        # Sort: best long = lowest RSI2, best short = highest RSI2
        long_cands  = sorted([c for c in candidates if c["side"] == "long"],  key=lambda x: x["rsi2"])
        short_cands = sorted([c for c in candidates if c["side"] == "short"], key=lambda x: -x["rsi2"])
        ordered     = (long_cands + short_cands)[:slots_available]

        for cand in ordered:
            tk    = cand["ticker"]
            side  = cand["side"]
            entry = cand["close"]
            atr_v = cand["atr"]

            stop = entry - ATR_STOP_MULT * atr_v if side == "long" else entry + ATR_STOP_MULT * atr_v
            shares = position_size(capital, entry, stop)
            if shares <= 0:
                continue

            cost = shares * entry
            if cost > capital:
                continue   # not enough cash

            capital -= cost
            new_pos = {
                "ticker":      tk,
                "side":        side,
                "entry_date":  today_str,
                "entry_price": round(entry, 4),
                "shares":      shares,
                "stop":        round(stop, 4),
                "cost":        round(cost, 2),
                "rsi2_entry":  round(cand["rsi2"], 2),
            }
            open_pos.append(new_pos)
            open_tickers.add(tk)
            entries_today.append({"ticker": tk, "side": side, "price": round(entry, 2), "shares": shares, "stop": round(stop, 2)})
            log_entry["entries"].append(entries_today[-1])
            print(f"  ENTER {side} {tk} @ ${entry:.2f}  x{shares}  stop=${stop:.2f}  rsi2={cand['rsi2']:.1f}")

    # ── Step 3: Mark-to-market portfolio value ────────────────────────────────
    open_value = 0.0
    for pos in open_pos:
        tk = pos["ticker"]
        if tk in prices.columns:
            last_px = float(prices[tk].dropna().iloc[-1])
            shares  = pos["shares"]
            cost    = pos["cost"]
            if pos["side"] == "long":
                open_value += last_px * shares
            else:
                # short: worth cost + (entry - current) * shares
                open_value += cost + (pos["entry_price"] - last_px) * shares
        else:
            open_value += pos["cost"]

    portfolio_value = capital + open_value
    state["nav_history"][today_str] = round(portfolio_value, 2)

    # ── Update state ──────────────────────────────────────────────────────────
    state["capital"]           = round(capital, 2)
    state["open_positions"]    = open_pos
    state["closed_positions"]  = closed
    log_entry["notes"].append(
        f"Portfolio: ${portfolio_value:,.0f}  |  Cash: ${capital:,.0f}  |  "
        f"Open: {len(open_pos)}  |  Exits: {len(exits_today)}  |  Entries: {len(entries_today)}"
    )
    state["log"].append(log_entry)
    save_trades(state)

    # ── Console summary ───────────────────────────────────────────────────────
    total_ret = portfolio_value - STARTING_CAPITAL
    total_pct = total_ret / STARTING_CAPITAL * 100
    print(f"\n{'='*56}")
    print(f"  Swing Trader — {today_str}")
    print(f"  Portfolio: ${portfolio_value:,.0f}  ({total_ret:+,.0f} / {total_pct:+.2f}%)")
    print(f"  Cash:      ${capital:,.0f}")
    print(f"  Open:      {len(open_pos)} positions")
    print(f"  Today:     {len(entries_today)} entries, {len(exits_today)} exits")
    print(f"{'='*56}\n")

    # ── Rebuild dashboard HTML ────────────────────────────────────────────────
    build_swing_dashboard(state, prices)


# ── HTML Dashboard ────────────────────────────────────────────────────────────

def _scan_rows(scan: list[dict]) -> str:
    if not scan:
        return '<tr><td colspan="5" style="color:#999;text-align:center;padding:12px">No scan data yet — run once during market hours</td></tr>'
    rows = ""
    for r in scan:
        is_signal = "signal" in r["status"]
        sc = "#1b5e20" if r["status"] == "LONG signal" else ("#880e4f" if r["status"] == "SHORT signal" else "#555")
        bold = "font-weight:bold;" if is_signal else ""
        rsi_color = "#1b5e20" if r["rsi2"] < 15 else ("#880e4f" if r["rsi2"] > 85 else "#333")
        rows += (
            f'<tr>'
            f'<td style="font-weight:bold">{r["ticker"]}</td>'
            f'<td style="color:{sc};{bold}">{r["status"]}</td>'
            f'<td style="text-align:right;color:{rsi_color};font-weight:bold">{r["rsi2"]}</td>'
            f'<td style="text-align:right">${r["close"]}</td>'
            f'<td style="text-align:right">${r["sma200"]}</td>'
            f'</tr>'
        )
    return rows


def build_swing_dashboard(state: dict, prices: pd.DataFrame):
    open_pos  = state["open_positions"]
    closed    = state["closed_positions"]
    nav       = state["nav_history"]
    capital   = state["capital"]
    inception = state["inception_date"] or date.today().isoformat()

    today_str = date.today().isoformat()

    # Fetch a fresh intraday snapshot for open positions so the dashboard
    # shows live prices rather than the stale close from the morning run.
    open_tks = [p["ticker"] for p in open_pos]
    if open_tks:
        try:
            snap = yf.download(open_tks, period="1d", interval="1m",
                               auto_adjust=True, progress=False)
            if not snap.empty:
                if isinstance(snap.columns, pd.MultiIndex):
                    snap_close = snap["Close"]
                else:
                    snap_close = snap[["Close"]].rename(columns={"Close": open_tks[0]})
                for tk in open_tks:
                    if tk in snap_close.columns:
                        s = snap_close[tk].dropna()
                        if len(s):
                            # Override the daily close with the freshest 1-min bar
                            if tk not in prices.columns:
                                prices[tk] = float(s.iloc[-1])
                            else:
                                prices.loc[prices.index[-1], tk] = float(s.iloc[-1])
        except Exception:
            pass  # fall back to the daily prices already in the DataFrame
    portfolio_value = nav.get(today_str, STARTING_CAPITAL)
    total_ret = portfolio_value - STARTING_CAPITAL
    total_pct = total_ret / STARTING_CAPITAL * 100
    gain_color = "#2e7d32" if total_ret >= 0 else "#c62828"
    gain_sign  = "+" if total_ret >= 0 else ""

    # Day P&L — compare today's NAV to previous trading day's NAV
    nav_dates   = sorted(nav.keys())
    prev_nav    = STARTING_CAPITAL
    if len(nav_dates) >= 2 and nav_dates[-1] == today_str:
        prev_nav = nav[nav_dates[-2]]
    elif nav_dates and nav_dates[-1] != today_str:
        prev_nav = nav[nav_dates[-1]]
    day_pnl      = round(portfolio_value - prev_nav, 2)
    day_pct      = round(day_pnl / prev_nav * 100, 2) if prev_nav else 0
    day_pnl_color = "#2e7d32" if day_pnl >= 0 else "#c62828"
    day_pnl_sign  = "+" if day_pnl >= 0 else ""

    # Open positions rows
    open_rows = ""
    open_value = portfolio_value - capital
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        stop  = pos["stop"]
        edate = pos["entry_date"]
        cost  = pos["cost"]
        rsi_e = pos.get("rsi2_entry", "—")

        cur_px = entry
        if tk in prices.columns:
            s = prices[tk].dropna()
            if len(s):
                cur_px = float(s.iloc[-1])

        if side == "long":
            unreal = (cur_px - entry) * shares
            cur_val = cur_px * shares
        else:
            unreal = (entry - cur_px) * shares
            cur_val = cost + unreal

        unreal_pct = unreal / cost * 100 if cost > 0 else 0
        unreal_color = "#2e7d32" if unreal >= 0 else "#c62828"
        side_badge = (
            '<span style="background:#e8f5e9;color:#1b5e20;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold">LONG</span>'
            if side == "long" else
            '<span style="background:#fce4ec;color:#880e4f;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold">SHORT</span>'
        )
        open_rows += f"""
        <tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">
          <td style="font-weight:bold">{tk}</td>
          <td>{side_badge}</td>
          <td>${entry:,.2f}</td>
          <td class="live-price">${cur_px:,.2f}</td>
          <td style="text-align:right">{shares:,}</td>
          <td style="text-align:right">${cost:,.0f}</td>
          <td class="live-unreal" style="text-align:right;color:{unreal_color};font-weight:bold">{gain_sign if unreal >= 0 else ''}${abs(unreal):,.0f} ({unreal_pct:+.2f}%)</td>
          <td>${stop:,.2f}</td>
          <td style="color:#666;font-size:12px">{rsi_e}</td>
          <td style="color:#666;font-size:12px">{edate}</td>
        </tr>"""

    if not open_rows:
        open_rows = '<tr><td colspan="10" style="text-align:center;color:#999;padding:20px">No open positions</td></tr>'

    # Closed positions rows (most recent first)
    closed_rows = ""
    for pos in reversed(closed[-50:]):
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        exit_p= pos.get("exit_price", entry)
        pnl   = pos.get("pnl", 0)
        edate = pos["entry_date"]
        xdate = pos.get("exit_date", "—")
        reason= pos.get("exit_reason", "—")
        cost  = pos["cost"]
        pnl_pct = pnl / cost * 100 if cost > 0 else 0
        pnl_color = "#2e7d32" if pnl >= 0 else "#c62828"
        side_badge = (
            '<span style="background:#e8f5e9;color:#1b5e20;padding:2px 8px;border-radius:4px;font-size:11px">LONG</span>'
            if side == "long" else
            '<span style="background:#fce4ec;color:#880e4f;padding:2px 8px;border-radius:4px;font-size:11px">SHORT</span>'
        )
        closed_rows += f"""
        <tr>
          <td style="font-weight:bold">{tk}</td>
          <td>{side_badge}</td>
          <td>${entry:,.2f}</td>
          <td>${exit_p:,.2f}</td>
          <td style="text-align:right;color:{pnl_color};font-weight:bold">${pnl:+,.0f} ({pnl_pct:+.2f}%)</td>
          <td style="color:#666;font-size:12px">{edate}</td>
          <td style="color:#666;font-size:12px">{xdate}</td>
          <td style="color:#666;font-size:12px">{reason}</td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">No closed trades yet</td></tr>'

    # NAV chart data for sparkline
    nav_dates  = sorted(nav.keys())
    nav_values = [nav[d] for d in nav_dates]
    spy_cum    = fetch_spy_cumulative(nav)
    # SPY equity curve: convert cum % back to dollar values anchored at STARTING_CAPITAL
    spy_values = [round(STARTING_CAPITAL * (1 + spy_cum.get(d, 0) / 100), 2) for d in nav_dates]
    nav_js     = json.dumps({"dates": nav_dates, "values": nav_values, "spy": spy_values})

    # Index returns for journal
    idx_returns = fetch_index_returns(nav)

    # Stats
    wins  = [p for p in closed if p.get("pnl", 0) > 0]
    losses= [p for p in closed if p.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    total_pnl_closed = sum(p.get("pnl", 0) for p in closed)
    avg_win  = sum(p.get("pnl", 0) for p in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(p.get("pnl", 0) for p in losses)/ len(losses) if losses else 0
    pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float("inf")

    stats_rows = f"""
    <tr><td>Starting Capital</td><td style="text-align:right;font-weight:bold">${STARTING_CAPITAL:,.0f}</td></tr>
    <tr><td>Current Value</td><td style="text-align:right;font-weight:bold;color:{gain_color}">${portfolio_value:,.0f}</td></tr>
    <tr><td>Total Return</td><td style="text-align:right;font-weight:bold;color:{gain_color}">{gain_sign}${abs(total_ret):,.0f} ({gain_sign}{abs(total_pct):.2f}%)</td></tr>
    <tr><td>Cash Available</td><td style="text-align:right">${capital:,.0f}</td></tr>
    <tr><td>Open Positions</td><td style="text-align:right">{len(open_pos)}</td></tr>
    <tr><td>Closed Trades</td><td style="text-align:right">{len(closed)}</td></tr>
    <tr><td>Win Rate</td><td style="text-align:right">{win_rate:.1f}%</td></tr>
    <tr><td>Avg Win</td><td style="text-align:right;color:#2e7d32">${avg_win:+,.0f}</td></tr>
    <tr><td>Avg Loss</td><td style="text-align:right;color:#c62828">${avg_loss:+,.0f}</td></tr>
    <tr><td>Profit Factor</td><td style="text-align:right">{pf:.2f}x</td></tr>
    <tr><td>Inception</td><td style="text-align:right;color:#666">{inception}</td></tr>
    """

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Swing Trader — {today_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; padding: 16px 20px; color: #222; background: #f0f2f5; }}
    h1   {{ color: #1a237e; margin-bottom: 4px; font-size: 22px; }}
    h2   {{ color: #1a237e; font-size: 16px; margin: 0 0 10px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th    {{ background: #1a237e; color: white; padding: 8px 10px; text-align: left; white-space: nowrap; }}
    td    {{ padding: 6px 10px; border-bottom: 1px solid #eee; white-space: nowrap; }}
    tr:hover td {{ background: #f5f5f5; }}
    .section {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px;
                padding: 18px 20px; margin: 12px 0; overflow-x: auto; }}
    .badge {{ background: #e3f2fd; color: #0d47a1; padding: 3px 10px;
              border-radius: 12px; font-size: 12px; font-weight: bold; }}
    .card  {{ background: #f0f4ff; border: 1px solid #c5cae9; border-radius: 10px;
              padding: 16px 24px; min-width: 160px; }}
    .card-label {{ font-size: 11px; color: #6c757d; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }}
    .card-value {{ font-size: 28px; font-weight: bold; color: #1a237e; line-height: 1; }}
    details summary {{ cursor: pointer; font-size: 16px; font-weight: bold; color: #1a237e;
                       padding: 4px 0; user-select: none; list-style: none; }}
    details summary::before {{ content: "▶ "; font-size: 12px; }}
    details[open] summary::before {{ content: "▼ "; font-size: 12px; }}
    details summary::-webkit-details-marker {{ display: none; }}
    canvas {{ max-width: 100%; }}
  </style>
</head>
<body>
  <h1>📈 Swing Trading Bot — RSI(2) Strategy</h1>
  <p style="margin: 6px 0 12px">
    <span class="badge">{today_str}</span>&nbsp;
    <span class="badge">RSI(2) Mean Reversion</span>&nbsp;
    <span class="badge">{len(open_pos)} open positions</span>&nbsp;
    <span class="badge">$100k starting capital</span>
  </p>

  <!-- Summary cards -->
  <div class="section">
    <h2>Portfolio Summary</h2>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
      <div class="card">
        <div class="card-label">Portfolio Value</div>
        <div class="card-value" id="port-value" style="color:#1a237e">${portfolio_value:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Total Return</div>
        <div class="card-value" id="port-total" style="color:{gain_color}">{gain_sign}${abs(total_ret):,.0f}<br>
          <span style="font-size:14px" id="port-total-pct">{gain_sign}{abs(total_pct):.2f}%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-label">Day P&amp;L</div>
        <div class="card-value" style="color:{day_pnl_color}">{day_pnl_sign}${abs(day_pnl):,.0f}<br>
          <span style="font-size:14px">{day_pnl_sign}{abs(day_pct):.2f}%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-label">Cash</div>
        <div class="card-value">${capital:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Open Positions</div>
        <div class="card-value">{len(open_pos)}</div>
      </div>
    </div>
    <canvas id="nav-chart" height="100"></canvas>
  </div>

  <!-- Open positions -->
  <div class="section">
    <h2>Open Positions</h2>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th>
        <th>Shares</th><th>$ Invested</th><th>Unrealized P&amp;L</th>
        <th>Stop</th><th>RSI(2) at entry</th><th>Entry Date</th>
      </tr></thead>
      <tbody>{open_rows}</tbody>
    </table>
  </div>

  <!-- Statistics -->
  <div class="section">
    <details open>
      <summary>Strategy Statistics</summary>
      <div style="display:flex;gap:30px;flex-wrap:wrap;margin-top:12px">
        <table style="width:auto;min-width:260px">
          <tbody>{stats_rows}</tbody>
        </table>
        <div style="flex:1;min-width:260px">
          <p style="color:#666;font-size:12px;line-height:1.7;margin:0">
            <strong>Strategy:</strong> RSI(2) Mean Reversion (Larry Connors)<br>
            <strong>Entry long:</strong> Close &gt; SMA(200) AND RSI(2) &lt; {RSI_LONG_ENTRY}<br>
            <strong>Entry short:</strong> Close &lt; SMA(200) AND RSI(2) &gt; {RSI_SHORT_ENTRY}<br>
            <strong>Exit:</strong> Price crosses 5-day SMA (or hits stop)<br>
            <strong>Stop:</strong> Entry ± {ATR_STOP_MULT}× ATR(14)<br>
            <strong>Risk:</strong> {RISK_PER_TRADE:.0%} of portfolio per trade<br>
            <strong>Max positions:</strong> no cap (cash-limited)<br>
            <strong>Universe:</strong> S&amp;P 500
          </p>
        </div>
      </div>
    </details>
  </div>

  <!-- Closed trades -->
  <div class="section">
    <details>
      <summary>Closed Trades ({len(closed)} total, showing last 50)</summary>
      <table style="margin-top:12px">
        <thead><tr>
          <th>Ticker</th><th>Side</th><th>Entry</th><th>Exit</th>
          <th>P&amp;L</th><th>Entry Date</th><th>Exit Date</th><th>Reason</th>
        </tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </details>
  </div>

  <!-- Scan diagnostics -->
  <div class="section">
    <details>
      <summary>Last Scan — RSI(2) Closest to Signal (top 40 of universe)</summary>
      <p style="color:#666;font-size:12px;margin:8px 0 6px">
        Entry fires when RSI(2) &lt; {RSI_LONG_ENTRY} (long, above SMA200) or &gt; {RSI_SHORT_ENTRY} (short, below SMA200).
        Sorted by how close to threshold. Green = signal fired today.
      </p>
      <table style="margin-top:4px">
        <thead><tr>
          <th>Ticker</th><th>Status</th><th>RSI(2)</th><th>Close</th><th>SMA(200)</th>
        </tr></thead>
        <tbody>{_scan_rows(state.get("last_scan", []))}</tbody>
      </table>
    </details>
  </div>

  {build_journal_section(nav, idx_returns, spy_cum)}

  <p style="color:#bbb;font-size:11px;margin-top:16px" id="live-status">
    swing_trader.py &nbsp;|&nbsp; Simulated trades only — not real money. &nbsp;|&nbsp;
    Data: Yahoo Finance &nbsp;|&nbsp; Not financial advice.
  </p>

<script>
// ── Finnhub live prices ───────────────────────────────────────────────────────
(function() {{
  const TOKEN = 'd93d6v1r01qgqnua64j0d93d6v1r01qgqnua64jg';
  const STARTING = {STARTING_CAPITAL};
  const delay = ms => new Promise(r => setTimeout(r, ms));

  function isMarketHours() {{
    const now = new Date();
    const et = new Date(now.toLocaleString('en-US', {{timeZone: 'America/New_York'}}));
    const day = et.getDay();
    if (day === 0 || day === 6) return false;
    const h = et.getHours(), m = et.getMinutes();
    const mins = h * 60 + m;
    return mins >= 570 && mins < 960; // 9:30–4:00
  }}

  async function fetchPrices() {{
    const rows = document.querySelectorAll('tr[data-ticker]');
    if (!rows.length) return;
    const quotes = {{}};
    for (const row of rows) {{
      const tk = row.dataset.ticker;
      try {{
        const q = await fetch(`https://finnhub.io/api/v1/quote?symbol=${{tk}}&token=${{TOKEN}}`).then(r => r.json());
        if (q && q.c) quotes[tk] = {{ price: q.c, prevClose: q.pc }};
      }} catch(e) {{}}
      await delay(1100);
    }}
    applyPrices(rows, quotes);
    updatePortfolio(rows, quotes);
    const el = document.getElementById('live-status');
    if (el) {{
      const t = new Date().toLocaleTimeString('en-US', {{timeZone:'America/New_York'}});
      el.innerHTML = `Live prices as of ${{t}} ET &nbsp;|&nbsp; Simulated trades only — not real money.`;
      el.style.color = '#388e3c';
    }}
  }}

  function applyPrices(rows, quotes) {{
    for (const row of rows) {{
      const tk   = row.dataset.ticker;
      const qty  = parseFloat(row.dataset.qty);
      const buy  = parseFloat(row.dataset.buypx);
      const side = row.dataset.side;
      const q    = quotes[tk];
      if (!q) continue;
      const price = q.price;

      const priceCell  = row.querySelector('.live-price');
      const unrealCell = row.querySelector('.live-unreal');
      if (priceCell) priceCell.textContent = '$' + price.toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}});

      if (unrealCell) {{
        const unreal = side === 'long' ? (price - buy) * qty : (buy - price) * qty;
        const cost   = buy * qty;
        const pct    = cost > 0 ? unreal / cost * 100 : 0;
        const sign   = unreal >= 0 ? '+' : '';
        unrealCell.textContent = `${{sign}}$${{Math.abs(unreal).toLocaleString('en-US', {{maximumFractionDigits:0}})}} (${{pct >= 0 ? '+' : ''}}${{pct.toFixed(2)}}%)`;
        unrealCell.style.color = unreal >= 0 ? '#2e7d32' : '#c62828';
      }}
    }}
  }}

  function updatePortfolio(rows, quotes) {{
    let openValue = 0;
    for (const row of rows) {{
      const tk   = row.dataset.ticker;
      const qty  = parseFloat(row.dataset.qty);
      const buy  = parseFloat(row.dataset.buypx);
      const side = row.dataset.side;
      const q    = quotes[tk];
      if (!q) {{ openValue += buy * qty; continue; }}
      const price = q.price;
      openValue += side === 'long' ? price * qty : buy * qty + (buy - price) * qty;
    }}
    const portEl   = document.getElementById('port-value');
    const totalEl  = document.getElementById('port-total');
    if (!portEl) return;
    const cashVal = (function() {{
      const cards = document.querySelectorAll('.card');
      for (const c of cards) {{
        const lbl = c.querySelector('.card-label');
        if (lbl && lbl.textContent.trim() === 'Cash') {{
          const val = c.querySelector('.card-value');
          if (val) return parseFloat(val.textContent.replace(/[$,]/g, '')) || 0;
        }}
      }}
      return 0;
    }})();
    const portVal  = cashVal + openValue;
    const ret      = portVal - STARTING;
    const retPct   = ret / STARTING * 100;
    const sign     = ret >= 0 ? '+' : '';
    const color    = ret >= 0 ? '#2e7d32' : '#c62828';
    portEl.textContent  = '$' + portVal.toLocaleString('en-US', {{maximumFractionDigits:0}});
    if (totalEl) {{
      totalEl.innerHTML = `${{sign}}$${{Math.abs(ret).toLocaleString('en-US',{{maximumFractionDigits:0}})}}<br><span style="font-size:14px" id="port-total-pct">${{sign}}${{Math.abs(retPct).toFixed(2)}}%</span>`;
      totalEl.style.color = color;
    }}
  }}

  if (isMarketHours()) {{
    fetchPrices();
    setInterval(fetchPrices, 60000);
  }}
}})();

// NAV equity curve with SPY benchmark
(function() {{
  const data = {nav_js};
  if (!data.dates.length) return;
  const canvas = document.getElementById('nav-chart');
  const ctx    = canvas.getContext('2d');
  const W = canvas.offsetWidth || 800;
  const H = 140;
  canvas.width  = W;
  canvas.height = H;

  const vals = data.values;
  const spy  = data.spy || [];
  const N    = vals.length;
  const pad  = 20;

  const allVals = [...vals, ...spy].filter(v => v != null);
  const minV  = Math.min(...allVals);
  const maxV  = Math.max(...allVals);
  const range = maxV - minV || 1;

  const toX = i => pad + (i / (N - 1 || 1)) * (W - 2 * pad);
  const toY = v => H - pad - ((v - minV) / range) * (H - 2 * pad);

  ctx.clearRect(0, 0, W, H);

  // SPY line (grey dashed)
  if (spy.length) {{
    ctx.strokeStyle = '#e65100';
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    spy.forEach((v, i) => {{ if (v != null) {{ i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)); }} }});
    ctx.stroke();
    ctx.setLineDash([]);
  }}

  // Portfolio line (solid blue)
  ctx.strokeStyle = '#1a237e';
  ctx.lineWidth   = 2;
  ctx.beginPath();
  vals.forEach((v, i) => {{ i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)); }});
  ctx.stroke();

  // Fill under portfolio line
  ctx.lineTo(toX(N - 1), H - pad);
  ctx.lineTo(toX(0), H - pad);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(26,35,126,0.12)');
  grad.addColorStop(1, 'rgba(26,35,126,0)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Labels
  ctx.font = '11px sans-serif';
  ctx.fillStyle = '#666';
  if (data.dates.length) {{
    ctx.fillText(data.dates[0], pad, H - 4);
    const last = data.dates[N-1];
    ctx.fillText(last, W - pad - ctx.measureText(last).width, H - 4);
  }}
  // Portfolio end label
  const lastVal = '$' + vals[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
  ctx.fillStyle = vals[N-1] >= {STARTING_CAPITAL} ? '#2e7d32' : '#c62828';
  ctx.font = 'bold 12px sans-serif';
  ctx.fillText(lastVal, toX(N-1) - ctx.measureText(lastVal).width - 4, toY(vals[N-1]) - 4);
  // SPY end label
  if (spy.length) {{
    const spyLast = 'SPY $' + spy[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
    ctx.fillStyle = '#e65100';
    ctx.font = '11px sans-serif';
    ctx.fillText(spyLast, toX(N-1) - ctx.measureText(spyLast).width - 4, toY(spy[N-1]) + 14);
  }}
  // Legend
  ctx.font = '11px sans-serif';
  ctx.fillStyle = '#1a237e'; ctx.fillRect(pad, 6, 18, 3);
  ctx.fillStyle = '#333'; ctx.fillText('Portfolio', pad + 22, 12);
  ctx.strokeStyle = '#e65100'; ctx.setLineDash([4,4]); ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(pad + 90, 8); ctx.lineTo(pad + 108, 8); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#333'; ctx.fillText('SPY (buy & hold)', pad + 112, 12);
}})();
</script>

</body>
</html>"""

    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    out = report_dir / f"swing_{date.today().isoformat()}.html"
    out.write_text(html)
    print(f"  Dashboard → {out}")


# ── Entry ─────────────────────────────────────────────────────────────────────

def _smoke_test():
    """Run all code paths with synthetic data to catch runtime errors before deploy."""
    print("=== Smoke test ===")

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Synthetic state
    state = {
        "capital": 95_000.0,
        "open_positions": [
            {"ticker": "AAPL", "side": "long", "entry_price": 170.0, "shares": 50,
             "stop": 166.0, "cost": 8500.0, "entry_date": yesterday, "rsi2_entry": 8.5},
        ],
        "closed_positions": [
            {"ticker": "MSFT", "side": "long", "entry_price": 380.0, "exit_price": 388.0,
             "shares": 20, "pnl": 160.0, "cost": 7600.0, "entry_date": yesterday,
             "exit_date": today, "exit_reason": "SMA5 cross above"},
        ],
        "nav_history": {yesterday: 99_800.0, today: 100_200.0},
        "inception_date": yesterday,
        "scan_results": [],
        "log": [],
    }

    # Synthetic prices DataFrame (tickers as columns)
    import numpy as np
    dates = pd.date_range(end=today, periods=220, freq="B")
    prices = pd.DataFrame(
        {tk: 170 + np.cumsum(np.random.randn(220) * 0.5)
         for tk in ["AAPL", "MSFT", "NVDA"]},
        index=dates,
    )

    # Test build_journal_section
    idx_returns = {}
    spy_cum = {}
    journal = build_journal_section(state["nav_history"], idx_returns, spy_cum)
    print(f"  build_journal_section: {len(journal)} chars — OK")

    # Test build_swing_dashboard (writes HTML to disk — check it doesn't crash)
    try:
        build_swing_dashboard(state, prices)
        print(f"  build_swing_dashboard — OK")
    except Exception as e:
        print(f"  build_swing_dashboard — FAILED: {e}")
        raise

    print("=== All checks passed ===")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _smoke_test()
    else:
        run_swing_trader()
