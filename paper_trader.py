"""
Paper Trading Bot — RSI(2) Mean Reversion Strategy

Rules (Larry Connors):
  ENTRY long : close > 200-day SMA  AND  RSI(2) < 10
  EXIT long  : close > 5-day SMA
  ENTRY short: close < 200-day SMA  AND  RSI(2) > 90
  EXIT short : close < 5-day SMA
  VWAP filter: only long if current price is below VWAP (approximated from daily data)

Universe : S&P 500 (same tickers as live_monitor.py)
Capital  : $100,000 starting, 2% risk per trade, max 10 open positions
Risk     : stop = 2× ATR(14) below entry; max 2% portfolio loss per trade
Runs     : daily at 10am ET via GitHub Actions (paper_trading.yml)
State    : paper_trades.json  (all open + closed positions + NAV curve)
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
MAX_POSITIONS     = 10         # concurrent long + short positions
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
TRADES_FILE = BASE_DIR / "paper_trades.json"

WIKI_URL = (
    "https://en.wikipedia.org/w/index.php"
    "?action=raw&section=0&title=List_of_S%26P_500_companies"
)

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
        resp = requests.get(WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        tickers = []
        for line in resp.text.splitlines():
            if "||" in line:
                parts = line.split("||")
                if len(parts) > 1:
                    tk = parts[0].replace("|", "").strip()
                    if tk and 1 <= len(tk) <= 5 and tk.isupper() and tk.isalpha():
                        tickers.append(tk.replace(".", "-"))
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

def run_paper_trader():
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

    if slots_available > 0:
        candidates = []
        for tk in universe[:300]:
            if tk in open_tickers:
                continue
            sig = compute_signals(prices, tk)
            if sig is None:
                continue
            close, sma200, rsi2, atr_val = sig["close"], sig["sma200"], sig["rsi2"], sig["atr"]

            # Skip cheap / illiquid stocks
            if close < MIN_PRICE:
                continue

            long_signal  = close > sma200 and rsi2 < RSI_LONG_ENTRY
            short_signal = close < sma200 and rsi2 > RSI_SHORT_ENTRY

            if long_signal:
                candidates.append({"ticker": tk, "side": "long",  "rsi2": rsi2, "close": close, "atr": atr_val})
            elif short_signal:
                candidates.append({"ticker": tk, "side": "short", "rsi2": rsi2, "close": close, "atr": atr_val})

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
    print(f"  Paper Trader — {today_str}")
    print(f"  Portfolio: ${portfolio_value:,.0f}  ({total_ret:+,.0f} / {total_pct:+.2f}%)")
    print(f"  Cash:      ${capital:,.0f}")
    print(f"  Open:      {len(open_pos)} positions")
    print(f"  Today:     {len(entries_today)} entries, {len(exits_today)} exits")
    print(f"{'='*56}\n")

    # ── Rebuild dashboard HTML ────────────────────────────────────────────────
    build_paper_dashboard(state, prices)


# ── HTML Dashboard ────────────────────────────────────────────────────────────

def build_paper_dashboard(state: dict, prices: pd.DataFrame):
    open_pos  = state["open_positions"]
    closed    = state["closed_positions"]
    nav       = state["nav_history"]
    capital   = state["capital"]
    inception = state["inception_date"] or date.today().isoformat()

    today_str = date.today().isoformat()
    portfolio_value = nav.get(today_str, STARTING_CAPITAL)
    total_ret = portfolio_value - STARTING_CAPITAL
    total_pct = total_ret / STARTING_CAPITAL * 100
    gain_color = "#2e7d32" if total_ret >= 0 else "#c62828"
    gain_sign  = "+" if total_ret >= 0 else ""

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
        <tr>
          <td style="font-weight:bold">{tk}</td>
          <td>{side_badge}</td>
          <td>${entry:,.2f}</td>
          <td>${cur_px:,.2f}</td>
          <td style="text-align:right">{shares:,}</td>
          <td style="text-align:right">${cost:,.0f}</td>
          <td style="text-align:right;color:{unreal_color};font-weight:bold">{gain_sign if unreal >= 0 else ''}${abs(unreal):,.0f} ({unreal_pct:+.2f}%)</td>
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
    nav_js     = json.dumps({"dates": nav_dates, "values": nav_values})

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
  <title>Paper Trader — {today_str}</title>
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
  <h1>📈 Paper Trading Bot — RSI(2) Strategy</h1>
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
        <div class="card-value" style="color:#1a237e">${portfolio_value:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Total Return</div>
        <div class="card-value" style="color:{gain_color}">{gain_sign}${abs(total_ret):,.0f}<br>
          <span style="font-size:14px">{gain_sign}{abs(total_pct):.2f}%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-label">Cash</div>
        <div class="card-value">${capital:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Open Positions</div>
        <div class="card-value">{len(open_pos)} / {MAX_POSITIONS}</div>
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
            <strong>Max positions:</strong> {MAX_POSITIONS}<br>
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

  <p style="color:#bbb;font-size:11px;margin-top:16px">
    paper_trader.py &nbsp;|&nbsp; Simulated trades only — not real money. &nbsp;|&nbsp;
    Data: Yahoo Finance &nbsp;|&nbsp; Not financial advice.
  </p>

<script>
// NAV equity curve
(function() {{
  const data = {nav_js};
  if (!data.dates.length) return;
  const canvas = document.getElementById('nav-chart');
  const ctx    = canvas.getContext('2d');
  const W = canvas.offsetWidth || 800;
  const H = 120;
  canvas.width  = W;
  canvas.height = H;

  const vals  = data.values;
  const N     = vals.length;
  const minV  = Math.min(...vals);
  const maxV  = Math.max(...vals);
  const range = maxV - minV || 1;
  const pad   = 10;

  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = '#1a237e';
  ctx.lineWidth   = 2;
  ctx.beginPath();
  vals.forEach((v, i) => {{
    const x = pad + (i / (N - 1 || 1)) * (W - 2 * pad);
    const y = H - pad - ((v - minV) / range) * (H - 2 * pad);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // Fill gradient
  ctx.lineTo(pad + (W - 2 * pad), H - pad);
  ctx.lineTo(pad, H - pad);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(26,35,126,0.15)');
  grad.addColorStop(1, 'rgba(26,35,126,0)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Start / end labels
  ctx.fillStyle = '#666';
  ctx.font = '11px sans-serif';
  if (data.dates.length) {{
    ctx.fillText(data.dates[0], pad, H - 1);
    const last = data.dates[N-1];
    ctx.fillText(last, W - pad - ctx.measureText(last).width, H - 1);
  }}
  const lastVal = '$' + vals[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
  ctx.fillStyle = vals[N-1] >= {STARTING_CAPITAL} ? '#2e7d32' : '#c62828';
  ctx.font = 'bold 13px sans-serif';
  ctx.fillText(lastVal, W - pad - ctx.measureText(lastVal).width, 20);
}})();
</script>

</body>
</html>"""

    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    out = report_dir / f"paper_{date.today().isoformat()}.html"
    out.write_text(html)
    print(f"  Dashboard → {out}")


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_paper_trader()
