"""
Intraday Paper Trader — Opening Range Breakout (ORB) Strategy

Rules:
  Opening Range = high/low of first 30 min (9:30–10:00 AM ET)
  ENTRY long  : 15-min close > ORB high  AND  volume > 1.2× 20-day avg bar volume
  ENTRY short : 15-min close < ORB low   AND  volume > 1.2× 20-day avg bar volume
  EXIT        : trailing stop (distance = 0.5× ORB width) — no fixed target, rides winners
  INIT STOP   : entry ∓ 0.5× ORB width
  FORCE CLOSE : all positions closed at 3:45 PM ET regardless

Capital : $100,000, 1% risk per trade, max 5 concurrent positions
Universe: 45 most liquid S&P 500 names (hardcoded for fast intraday download)
Runs    : every 15 min 9:30am–4pm ET via GitHub Actions (intraday_trading.yml)
State   : intraday_trades.json
"""

from __future__ import annotations

import json
import warnings
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
STARTING_CAPITAL  = 100_000.0
RISK_PER_TRADE    = 0.01       # 1% of portfolio at risk per trade
MAX_POSITIONS     = 9999  # no cap — limited only by available cash
ORB_MINUTES       = 30         # opening range window
ORB_STOP_MULT     = 0.5        # trail distance = 0.5× ORB width
VOLUME_FILTER     = 1.2        # require 1.2× avg bar volume on breakout bar
FORCE_CLOSE_TIME  = dt_time(15, 45)   # 3:45pm ET
ORB_END_TIME      = dt_time(10, 0)    # 10:00am ET — ORB window ends
MARKET_OPEN_TIME  = dt_time(9, 30)    # 9:30am ET

ET = ZoneInfo("America/New_York")

# Universe loaded dynamically from S&P 500 at startup (cached daily in sp500_cache.json)
INTRADAY_UNIVERSE: list[str] = []

BASE_DIR      = Path(__file__).parent
TRADES_FILE   = BASE_DIR / "intraday_trades.json"
SP500_CACHE   = BASE_DIR / "sp500_cache.json"

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 tickers via HTML table, cached for the day."""
    today = date.today().isoformat()
    if SP500_CACHE.exists():
        try:
            cached = json.loads(SP500_CACHE.read_text())
            if cached.get("date") == today and len(cached.get("tickers", [])) > 400:
                return cached["tickers"]
        except Exception:
            pass
    try:
        import requests
        from io import StringIO
        resp = requests.get(WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        tickers = (
            pd.read_html(StringIO(resp.text))[0]["Symbol"]
            .str.replace(".", "-", regex=False)
            .tolist()
        )
        if len(tickers) > 400:
            SP500_CACHE.write_text(json.dumps({"date": today, "tickers": tickers}))
            print(f"  S&P 500 universe: {len(tickers)} tickers (refreshed)")
            return tickers
    except Exception as e:
        print(f"  S&P 500 fetch error: {e}")
    if SP500_CACHE.exists():
        try:
            return json.loads(SP500_CACHE.read_text()).get("tickers", [])
        except Exception:
            pass
    return []

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return _fresh_state()

def _fresh_state() -> dict:
    return {
        "capital":          STARTING_CAPITAL,
        "open_positions":   [],
        "closed_today":     [],
        "all_closed":       [],
        "today":            None,
        "today_orb":        {},       # {ticker: {high, low, width}}
        "nav_history":      {},
        "inception_date":   None,
        "log":              [],
    }

def save_state(state: dict) -> None:
    TRADES_FILE.write_text(json.dumps(state, indent=2, default=str))

def migrate_positions(state: dict) -> None:
    """Migrate old fixed-target positions to trailing stop format."""
    for pos in state.get("open_positions", []):
        if "trail_dist" not in pos:
            orb_w = pos.get("orb_width", 0.5)
            pos["trail_dist"] = round(ORB_STOP_MULT * orb_w, 4)
            pos.pop("target", None)
        if "peak" not in pos:
            pos["peak"] = pos["entry_price"]

def reset_for_new_day(state: dict, today_str: str) -> None:
    """Move closed_today to all_closed and reset daily fields."""
    state["all_closed"].extend(state.get("closed_today", []))
    state["closed_today"]   = []
    state["open_positions"] = []
    state["today_orb"]      = {}
    state["today"]          = today_str
    if state["inception_date"] is None:
        state["inception_date"] = today_str

# ── Market phase ──────────────────────────────────────────────────────────────

def get_market_phase() -> str:
    now = datetime.now(ET).time()
    if now < MARKET_OPEN_TIME:
        return "pre_market"
    if now < ORB_END_TIME:
        return "orb_window"
    if now < FORCE_CLOSE_TIME:
        return "trading"
    if now <= dt_time(16, 0):
        return "force_close"
    return "after_hours"

# ── Price data ────────────────────────────────────────────────────────────────

def fetch_intraday(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download today's 5-min bars + 21 days of daily bars for avg volume."""
    result = {}
    try:
        raw5 = yf.download(
            tickers, period="1d", interval="5m",
            auto_adjust=True, progress=False,
        )
        if raw5.empty:
            return result
        raw5.index = pd.DatetimeIndex(raw5.index).tz_convert(ET)

        raw1d = yf.download(
            tickers, period="22d", interval="1d",
            auto_adjust=True, progress=False,
        )

        for tk in tickers:
            try:
                if isinstance(raw5.columns, pd.MultiIndex):
                    df5  = raw5.xs(tk, axis=1, level=1).dropna(how="all")
                    df1d = raw1d.xs(tk, axis=1, level=1).dropna(how="all") if not raw1d.empty else pd.DataFrame()
                else:
                    df5  = raw5.dropna(how="all")
                    df1d = raw1d.dropna(how="all") if not raw1d.empty else pd.DataFrame()
                if df5.empty:
                    continue
                # avg_bar_volume: use opening-hour bars (9:30–10:30) as the baseline.
                # Comparing a breakout bar to full-day avg penalises afternoon bars when
                # volume naturally fades — opening-hour avg is the correct reference for ORB.
                if "Volume" in df5.columns:
                    orb_bars = df5.between_time("09:30", "10:30")
                    if len(orb_bars) >= 3:
                        avg_bar_vol = float(orb_bars["Volume"].mean())
                    elif not df1d.empty and "Volume" in df1d.columns:
                        # fallback: daily vol / 13 bars in first hour
                        avg_bar_vol = float(df1d["Volume"].iloc[:-1].mean()) / 13.0
                    else:
                        avg_bar_vol = float(df5["Volume"].mean())
                else:
                    avg_bar_vol = 0
                result[tk] = {"bars": df5, "avg_bar_vol": avg_bar_vol}
            except Exception:
                continue
    except Exception as e:
        print(f"  Intraday fetch error: {e}")
    return result

# ── ORB computation ───────────────────────────────────────────────────────────

def compute_orb(data: dict[str, dict]) -> dict[str, dict]:
    """Compute ORB high/low from 9:30–9:59 bars for each ticker."""
    orb = {}
    for tk, td in data.items():
        bars = td["bars"]
        orb_bars = bars.between_time("09:30", "09:59")
        if orb_bars.empty or len(orb_bars) < 3:
            continue
        hi  = float(orb_bars["High"].max())
        lo  = float(orb_bars["Low"].min())
        orb[tk] = {"high": round(hi, 4), "low": round(lo, 4), "width": round(hi - lo, 4)}
    return orb

# ── Entry/exit logic ──────────────────────────────────────────────────────────

def check_exits(state: dict, data: dict[str, dict], force: bool = False) -> list[dict]:
    exits = []
    still_open = []
    capital = state["capital"]

    for pos in state["open_positions"]:
        tk     = pos["ticker"]
        side   = pos["side"]
        entry  = pos["entry_price"]
        shares = pos["shares"]
        stop   = pos["stop"]        # current trailing stop level
        cost   = pos["cost"]
        trail_dist = pos["trail_dist"]  # fixed trail distance in $

        # Get current price and bar high/low to catch intrabar touches
        cur_px = entry
        bar_hi = entry
        bar_lo = entry
        if tk in data:
            bars = data[tk]["bars"]
            if not bars.empty:
                cur_px = float(bars["Close"].iloc[-1])
                bar_hi = float(bars["High"].iloc[-1])
                bar_lo = float(bars["Low"].iloc[-1])

        if force:
            reason  = "force close 3:45pm"
            exit_px = cur_px
        elif side == "long":
            # Advance trail stop to highest point seen
            new_stop = round(bar_hi - trail_dist, 4)
            if new_stop > stop:
                pos["stop"] = new_stop
                pos["peak"] = round(bar_hi, 4)
                stop = new_stop
            if bar_lo <= stop:
                reason, exit_px = "trailing stop", stop
            else:
                still_open.append(pos)
                continue
        else:
            # Short: trail stop downward as price falls
            new_stop = round(bar_lo + trail_dist, 4)
            if new_stop < stop:
                pos["stop"] = new_stop
                pos["peak"] = round(bar_lo, 4)
                stop = new_stop
            if bar_hi >= stop:
                reason, exit_px = "trailing stop", stop
            else:
                still_open.append(pos)
                continue

        pnl = (exit_px - entry) * shares if side == "long" else (entry - exit_px) * shares
        capital += cost + pnl

        closed = {**pos, "exit_date": date.today().isoformat(),
                  "exit_price": round(exit_px, 4), "pnl": round(pnl, 2),
                  "exit_reason": reason, "exit_time": datetime.now(ET).strftime("%H:%M ET")}
        state["closed_today"].append(closed)
        exits.append({"ticker": tk, "side": side, "exit_price": round(exit_px, 2),
                       "pnl": round(pnl, 2), "reason": reason})
        print(f"  EXIT {side} {tk} @ ${exit_px:.2f}  PnL: ${pnl:+,.0f}  ({reason})")

    state["open_positions"] = still_open
    state["capital"] = round(capital, 2)
    return exits

def scan_entries(state: dict, data: dict[str, dict]) -> list[dict]:
    entries = []
    open_tks = {p["ticker"] for p in state["open_positions"]}
    slots    = MAX_POSITIONS - len(state["open_positions"])
    capital  = state["capital"]
    orb      = state["today_orb"]

    if slots <= 0:
        return []

    candidates = []
    # Iterate over all tickers that have data — same universe as diagnostics
    for tk in INTRADAY_UNIVERSE:
        if tk in open_tks:
            continue
        if tk not in data:
            continue
        orb_data = orb.get(tk)
        if orb_data is None:
            continue

        bars    = data[tk]["bars"]
        avg_vol = data[tk]["avg_bar_vol"]
        if bars.empty:
            continue

        # Only look at bars after ORB window
        post_orb = bars[bars.index.time >= ORB_END_TIME]
        if post_orb.empty:
            continue

        last_bar = post_orb.iloc[-1]
        close_px = float(last_bar["Close"])
        bar_vol  = float(last_bar["Volume"]) if "Volume" in last_bar else 0

        orb_hi   = orb_data["high"]
        orb_lo   = orb_data["low"]
        orb_w    = orb_data["width"]

        if orb_w < 0.01:
            continue

        vol_ok = (avg_vol == 0) or (bar_vol >= VOLUME_FILTER * avg_vol)
        long_signal  = close_px > orb_hi and vol_ok
        short_signal = close_px < orb_lo and vol_ok

        if long_signal:
            candidates.append({"ticker": tk, "side": "long",  "close": close_px,
                                "orb_hi": orb_hi, "orb_lo": orb_lo, "orb_w": orb_w,
                                "vol_ratio": bar_vol / avg_vol if avg_vol else 1})
        elif short_signal:
            candidates.append({"ticker": tk, "side": "short", "close": close_px,
                                "orb_hi": orb_hi, "orb_lo": orb_lo, "orb_w": orb_w,
                                "vol_ratio": bar_vol / avg_vol if avg_vol else 1})

    # Sort by volume ratio (strongest breakouts first)
    candidates.sort(key=lambda x: -x["vol_ratio"])

    for c in candidates[:slots]:
        tk    = c["ticker"]
        side  = c["side"]
        entry = c["close"]
        orb_w = c["orb_w"]

        trail_dist = round(ORB_STOP_MULT * orb_w, 4)
        if side == "long":
            stop = entry - trail_dist
        else:
            stop = entry + trail_dist

        per_share_risk = trail_dist
        if per_share_risk < 0.01:
            continue
        shares = int((capital * RISK_PER_TRADE) / per_share_risk)
        shares = min(shares, int(capital * 0.20 / entry))
        if shares <= 0:
            continue

        cost = shares * entry
        if cost > capital:
            continue

        capital -= cost
        entry_time = datetime.now(ET).strftime("%H:%M ET")
        pos = {
            "ticker":      tk,
            "side":        side,
            "entry_date":  date.today().isoformat(),
            "entry_time":  entry_time,
            "entry_price": round(entry, 4),
            "shares":      shares,
            "stop":        round(stop, 4),
            "trail_dist":  trail_dist,
            "peak":        round(entry, 4),
            "orb_width":   round(orb_w, 4),
            "cost":        round(cost, 2),
            "vol_ratio":   round(c["vol_ratio"], 2),
        }
        state["open_positions"].append(pos)
        open_tks.add(tk)
        entries.append({"ticker": tk, "side": side, "price": round(entry, 2), "shares": shares,
                         "stop": round(stop, 2), "trail_dist": round(trail_dist, 4)})
        print(f"  ENTER {side} {tk} @ ${entry:.2f}  x{shares}  target=${target:.2f}  stop=${stop:.2f}")

    state["capital"] = round(capital, 2)
    return entries

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
        rows += f"<tr><td style='white-space:nowrap;font-weight:bold;color:#e65100'>{day_label}</td>"
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
        Day % = portfolio NAV change that day (realized + open M2M).&nbsp;
        Cum % = total return since inception.&nbsp;
        SPY Cum % = buy-and-hold SPY return over the same period.&nbsp;
        vs S&P = your day % minus S&P 500 day % (green = beat the market).
      </p>
      <table>
        <thead>
          <tr>
            <th rowspan="2" style="vertical-align:bottom">Date</th>
            <th colspan="4" style="background:#bf360c;text-align:center;font-size:11px">Your Portfolio</th>
            <th colspan="5" style="background:#bf360c;text-align:center;font-size:11px">Benchmark</th>
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

# ── Mark to market ────────────────────────────────────────────────────────────

def mark_to_market(state: dict, data: dict[str, dict]) -> float:
    open_value = 0.0
    for pos in state["open_positions"]:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        cost  = pos["cost"]
        cur_px = entry
        if tk in data:
            bars = data[tk]["bars"]
            if not bars.empty:
                cur_px = float(bars["Close"].iloc[-1])
        if side == "long":
            open_value += cur_px * shares
        else:
            open_value += cost + (entry - cur_px) * shares
    return state["capital"] + open_value

# ── Main ──────────────────────────────────────────────────────────────────────

def scan_diagnostics(state: dict, data: dict[str, dict]) -> list[dict]:
    """Return per-ticker scan results for the dashboard diagnostic table."""
    orb      = state.get("today_orb", {})
    rows     = []
    open_tks = {p["ticker"] for p in state["open_positions"]}
    slots    = MAX_POSITIONS - len(open_tks)
    phase    = get_market_phase()

    for tk in INTRADAY_UNIVERSE:
        orb_data = orb.get(tk)
        if tk not in data:
            rows.append({"ticker": tk, "status": "no data", "close": None,
                         "orb_hi": None, "orb_lo": None, "vol_ratio": None, "note": ""})
            continue
        bars    = data[tk]["bars"]
        avg_vol = data[tk]["avg_bar_vol"]
        if bars.empty:
            rows.append({"ticker": tk, "status": "empty bars", "close": None,
                         "orb_hi": None, "orb_lo": None, "vol_ratio": None, "note": ""})
            continue
        close_px = float(bars["Close"].iloc[-1])
        bar_vol  = float(bars["Volume"].iloc[-1]) if "Volume" in bars.columns else 0
        vol_ratio = round(bar_vol / avg_vol, 2) if avg_vol > 0 else None

        if orb_data is None:
            rows.append({"ticker": tk, "status": "orb not ready", "close": round(close_px, 2),
                         "orb_hi": None, "orb_lo": None, "vol_ratio": vol_ratio, "note": "need ≥3 bars 9:30–9:59"})
            continue

        orb_hi = orb_data["high"]
        orb_lo = orb_data["low"]
        orb_w  = orb_data["width"]
        vol_ok = (avg_vol == 0) or (bar_vol >= VOLUME_FILTER * avg_vol)

        post_orb = bars[bars.index.time >= ORB_END_TIME]

        if tk in open_tks:
            status = "in position"
            note   = ""
        elif orb_w < 0.01:
            status = "orb too tight"
            note   = f"width=${orb_w:.4f}"
        elif close_px > orb_hi and vol_ok:
            if phase != "trading":
                status = "LONG signal"
                note   = f"⚠ not traded — phase={phase}"
            else:
                status = "LONG signal"
                note   = "✓ eligible for entry"
        elif close_px < orb_lo and vol_ok:
            if phase != "trading":
                status = "SHORT signal"
                note   = f"⚠ not traded — phase={phase}"
            else:
                status = "SHORT signal"
                note   = "✓ eligible for entry"
        elif close_px > orb_hi:
            status = "breakout — low vol"
            note   = f"vol={vol_ratio}× (need {VOLUME_FILTER}×)"
        elif close_px < orb_lo:
            status = "breakdown — low vol"
            note   = f"vol={vol_ratio}× (need {VOLUME_FILTER}×)"
        else:
            status = "inside range"
            note   = ""

        rows.append({"ticker": tk, "status": status, "close": round(close_px, 2),
                     "orb_hi": round(orb_hi, 2), "orb_lo": round(orb_lo, 2),
                     "orb_w": round(orb_w, 2), "vol_ratio": vol_ratio, "note": note})
    return rows


def run_intraday():
    global INTRADAY_UNIVERSE
    today_str = date.today().isoformat()
    now_str   = datetime.now(ET).strftime("%H:%M ET")
    phase     = get_market_phase()
    state     = load_state()

    if not INTRADAY_UNIVERSE:
        INTRADAY_UNIVERSE = get_sp500_tickers()
        if not INTRADAY_UNIVERSE:
            print("  Could not load S&P 500 universe — aborting")
            return

    migrate_positions(state)
    print(f"Intraday Trader — {today_str} {now_str}  phase={phase}  universe={len(INTRADAY_UNIVERSE)}")

    # Always reset state for new day
    if state.get("today") != today_str:
        print(f"  New trading day — resetting state")
        reset_for_new_day(state, today_str)

    log_entry = {"date": today_str, "time": now_str, "phase": phase, "entries": [], "exits": []}

    # Always fetch data so dashboard shows current snapshot
    print(f"  Fetching 5-min bars for {len(INTRADAY_UNIVERSE)} tickers …")
    data = fetch_intraday(INTRADAY_UNIVERSE)
    print(f"  Got data for {len(data)} tickers")

    if phase not in ("pre_market", "after_hours"):
        # Refresh ORB
        state["today_orb"] = compute_orb(data)
        print(f"  ORB computed for {len(state['today_orb'])} tickers")

        force = (phase == "force_close")

        if state["open_positions"]:
            exits = check_exits(state, data, force=force)
            log_entry["exits"] = exits

        if phase == "trading":
            entries = scan_entries(state, data)
            log_entry["entries"] = entries

    # Mark to market and save
    portfolio_value = mark_to_market(state, data)
    state["nav_history"][today_str] = round(portfolio_value, 2)

    n_open = len(state["open_positions"])
    n_closed_today = len(state["closed_today"])
    pnl_today = sum(p.get("pnl", 0) for p in state["closed_today"])
    log_entry["portfolio_value"] = round(portfolio_value, 2)
    log_entry["note"] = (f"${portfolio_value:,.0f} | cash=${state['capital']:,.0f} | "
                         f"open={n_open} | closed today={n_closed_today} | day PnL=${pnl_today:+,.0f}")
    state["log"].append(log_entry)
    save_state(state)

    total_ret = portfolio_value - STARTING_CAPITAL
    print(f"\n  Portfolio: ${portfolio_value:,.0f}  ({total_ret:+,.0f} / {total_ret/STARTING_CAPITAL:+.2%})")
    print(f"  Cash: ${state['capital']:,.0f}  |  Open: {n_open}  |  Today PnL: ${pnl_today:+,.0f}")

    diag = scan_diagnostics(state, data)
    build_intraday_dashboard(state, data, diag)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _diag_rows(diag: list[dict]) -> str:
    status_color = {
        "LONG signal":        "#1b5e20",
        "SHORT signal":       "#880e4f",
        "in position":        "#0d47a1",
        "breakout — low vol": "#e65100",
        "breakdown — low vol":"#e65100",
        "inside range":       "#555",
        "orb not ready":      "#999",
        "orb too tight":      "#999",
        "no data":            "#bbb",
        "empty bars":         "#bbb",
    }
    rows = ""
    for r in diag:
        sc   = status_color.get(r["status"], "#555")
        bold = "font-weight:bold;" if "signal" in r["status"] else ""
        note = r.get("note", "")
        note_color = "#c62828" if "⚠" in note else ("#2e7d32" if "✓" in note else "#888")
        rows += (
            f'<tr><td style="font-weight:bold">{r["ticker"]}</td>'
            f'<td style="color:{sc};{bold}">{r["status"]}</td>'
            f'<td style="text-align:right">{("$"+str(r["close"])) if r["close"] else "—"}</td>'
            f'<td style="text-align:right">{("$"+str(r.get("orb_hi",""))) if r.get("orb_hi") else "—"}</td>'
            f'<td style="text-align:right">{("$"+str(r.get("orb_lo",""))) if r.get("orb_lo") else "—"}</td>'
            f'<td style="text-align:right">{("$"+str(r.get("orb_w",""))) if r.get("orb_w") else "—"}</td>'
            f'<td style="text-align:right">{(str(r["vol_ratio"])+"×") if r["vol_ratio"] else "—"}</td>'
            f'<td style="color:{note_color};font-size:12px">{note}</td>'
            f'</tr>'
        )
    return rows or '<tr><td colspan="8" style="color:#999;text-align:center;padding:12px">No data yet</td></tr>'


def build_intraday_dashboard(state: dict, data: dict[str, dict], diag: list[dict] | None = None):
    today_str = date.today().isoformat()
    open_pos  = state["open_positions"]
    closed_td = state["closed_today"]
    idx_returns = fetch_index_returns(state.get("nav_history", {}))
    all_closed= state["all_closed"]
    nav       = state["nav_history"]
    capital   = state["capital"]
    inception = state["inception_date"] or today_str

    portfolio_value = nav.get(today_str, STARTING_CAPITAL)
    total_ret = portfolio_value - STARTING_CAPITAL
    total_pct = total_ret / STARTING_CAPITAL * 100
    gain_color = "#2e7d32" if total_ret >= 0 else "#c62828"
    gain_sign  = "+" if total_ret >= 0 else ""

    pnl_today = sum(p.get("pnl", 0) for p in closed_td)
    pnl_color = "#2e7d32" if pnl_today >= 0 else "#c62828"
    phase = get_market_phase()

    # Open positions rows
    open_rows = ""
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        stop  = pos["stop"]
        peak  = pos.get("peak", entry)
        orb_w = pos.get("orb_width", 0)

        cur_px = entry
        if tk in data:
            bars = data[tk]["bars"]
            if not bars.empty:
                cur_px = float(bars["Close"].iloc[-1])

        if side == "long":
            unreal = (cur_px - entry) * shares
        else:
            unreal = (entry - cur_px) * shares
        unreal_pct = unreal / pos["cost"] * 100 if pos["cost"] > 0 else 0
        uc = "#2e7d32" if unreal >= 0 else "#c62828"
        sb = ('<span style="background:#e8f5e9;color:#1b5e20;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold">LONG</span>'
              if side == "long" else
              '<span style="background:#fce4ec;color:#880e4f;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold">SHORT</span>')
        open_rows += f"""
        <tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">
          <td style="font-weight:bold">{tk}</td><td>{sb}</td>
          <td>${entry:,.2f}</td><td class="live-price">${cur_px:,.2f}</td>
          <td style="text-align:right">{shares:,}</td>
          <td class="live-unreal" style="text-align:right;color:{uc};font-weight:bold">${unreal:+,.0f} ({unreal_pct:+.2f}%)</td>
          <td style="color:#1b5e20">${peak:,.2f}</td><td style="color:#c62828">${stop:,.2f}</td>
          <td style="color:#666;font-size:12px">{pos.get('entry_time','—')}</td>
        </tr>"""
    if not open_rows:
        open_rows = '<tr><td colspan="9" style="text-align:center;color:#999;padding:20px">No open positions today</td></tr>'

    # Today's closed trades
    today_rows = ""
    for pos in reversed(closed_td):
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        exit_p= pos.get("exit_price", entry)
        pnl   = pos.get("pnl", 0)
        reason= pos.get("exit_reason", "—")
        pnl_c = "#2e7d32" if pnl >= 0 else "#c62828"
        sb = ('<span style="background:#e8f5e9;color:#1b5e20;padding:2px 8px;border-radius:4px;font-size:11px">LONG</span>'
              if side == "long" else
              '<span style="background:#fce4ec;color:#880e4f;padding:2px 8px;border-radius:4px;font-size:11px">SHORT</span>')
        today_rows += f"""
        <tr>
          <td style="font-weight:bold">{tk}</td><td>{sb}</td>
          <td>${entry:,.2f}</td><td>${exit_p:,.2f}</td>
          <td style="text-align:right;color:{pnl_c};font-weight:bold">${pnl:+,.0f}</td>
          <td style="color:#666;font-size:12px">{pos.get('entry_time','—')}</td>
          <td style="color:#666;font-size:12px">{pos.get('exit_time','—')}</td>
          <td style="color:#666;font-size:12px">{reason}</td>
        </tr>"""
    if not today_rows:
        today_rows = '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">No trades closed today</td></tr>'

    # All-time stats
    all_c = all_closed + closed_td
    wins  = [p for p in all_c if p.get("pnl", 0) > 0]
    losses= [p for p in all_c if p.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(all_c) * 100 if all_c else 0
    avg_win  = sum(p["pnl"] for p in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(p["pnl"] for p in losses) / len(losses) if losses else 0
    pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float("inf")

    nav_dates  = sorted(nav.keys())
    nav_values = [nav[d] for d in nav_dates]
    spy_cum    = fetch_spy_cumulative(nav)
    spy_values = [round(STARTING_CAPITAL * (1 + spy_cum.get(d, 0) / 100), 2) for d in nav_dates]
    nav_js = json.dumps({"dates": nav_dates, "values": nav_values, "spy": spy_values})

    phase_badge = {
        "pre_market":  ("Pre-market", "#666"),
        "orb_window":  ("ORB Window", "#e65100"),
        "trading":     ("Trading", "#1b5e20"),
        "force_close": ("Force Close", "#b71c1c"),
        "after_hours": ("After Hours", "#666"),
    }.get(phase, ("—", "#666"))

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Intraday Trader — {today_str}</title>
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
              padding: 16px 24px; min-width: 150px; }}
    .card-label {{ font-size: 11px; color: #6c757d; text-transform: uppercase;
                   letter-spacing: .5px; margin-bottom: 4px; }}
    .card-value {{ font-size: 26px; font-weight: bold; line-height: 1; }}
    details summary {{ cursor: pointer; font-size: 16px; font-weight: bold; color: #1a237e;
                       padding: 4px 0; user-select: none; list-style: none; }}
    details summary::before {{ content: "▶ "; font-size: 12px; }}
    details[open] summary::before {{ content: "▼ "; font-size: 12px; }}
    details summary::-webkit-details-marker {{ display: none; }}
  </style>
</head>
<body>
  <h1>⚡ Intraday Paper Trader — ORB Strategy</h1>
  <p style="margin: 6px 0 12px">
    <span class="badge">{today_str}</span>&nbsp;
    <span class="badge" style="background:#e8f5e9;color:{phase_badge[1]}">{phase_badge[0]}</span>&nbsp;
    <span class="badge">ORB {ORB_MINUTES}-min</span>&nbsp;
    <span class="badge">{len(open_pos)} open</span>&nbsp;
    <span class="badge" id="live-badge">Live prices every 30s</span>
  </p>

  <div class="section">
    <h2>Today's Summary</h2>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
      <div class="card">
        <div class="card-label">Portfolio Value</div>
        <div class="card-value" id="port-value" style="color:#1a237e">${portfolio_value:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Total Return</div>
        <div class="card-value" id="port-total" style="color:{gain_color}">{gain_sign}${abs(total_ret):,.0f}<br>
          <span style="font-size:14px" id="port-total-pct">{gain_sign}{abs(total_pct):.2f}%</span></div>
      </div>
      <div class="card">
        <div class="card-label">Day P&amp;L</div>
        <div class="card-value" id="port-today" style="color:{pnl_color}">${pnl_today:+,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Cash</div>
        <div class="card-value">${capital:,.0f}</div>
      </div>
      <div class="card">
        <div class="card-label">Win Rate (all-time)</div>
        <div class="card-value">{win_rate:.1f}%<br>
          <span style="font-size:12px;color:#666">PF: {pf:.2f}x</span></div>
      </div>
    </div>
    <canvas id="nav-chart" height="80"></canvas>
  </div>

  <div class="section">
    <h2>Open Positions</h2>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th>
        <th>Shares</th><th>Unrealized P&amp;L</th><th>Peak</th><th>Trail Stop</th><th>Entry Time</th>
      </tr></thead>
      <tbody>{open_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Today's Closed Trades</h2>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Side</th><th>Entry</th><th>Exit</th>
        <th>P&amp;L</th><th>Entry Time</th><th>Exit Time</th><th>Reason</th>
      </tr></thead>
      <tbody>{today_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <details>
      <summary>Strategy Info &amp; All-Time Stats</summary>
      <div style="display:flex;gap:30px;flex-wrap:wrap;margin-top:12px">
        <table style="width:auto;min-width:240px">
          <tr><td>Starting Capital</td><td style="text-align:right;font-weight:bold">${STARTING_CAPITAL:,.0f}</td></tr>
          <tr><td>Total Closed Trades</td><td style="text-align:right">{len(all_c)}</td></tr>
          <tr><td>Win Rate</td><td style="text-align:right">{win_rate:.1f}%</td></tr>
          <tr><td>Avg Win</td><td style="text-align:right;color:#2e7d32">${avg_win:+,.0f}</td></tr>
          <tr><td>Avg Loss</td><td style="text-align:right;color:#c62828">${avg_loss:+,.0f}</td></tr>
          <tr><td>Profit Factor</td><td style="text-align:right">{pf:.2f}x</td></tr>
          <tr><td>Inception</td><td style="text-align:right;color:#666">{inception}</td></tr>
        </table>
        <div style="flex:1;min-width:240px">
          <p style="color:#666;font-size:12px;line-height:1.8;margin:0">
            <strong>Strategy:</strong> Opening Range Breakout (ORB)<br>
            <strong>ORB window:</strong> 9:30–10:00 AM ET (first 30 min)<br>
            <strong>Entry:</strong> 15-min close breaks ORB high/low + volume ≥ 1.2× avg<br>
            <strong>Exit:</strong> Trailing stop — rides winners, distance = 0.5× ORB width<br>
            <strong>Initial stop:</strong> Entry ∓ 0.5× ORB width<br>
            <strong>Force close:</strong> 3:45 PM ET<br>
            <strong>Risk:</strong> {RISK_PER_TRADE:.0%} per trade, no position cap (cash-limited)<br>
            <strong>Universe:</strong> 45 most liquid S&amp;P 500 names
          </p>
        </div>
      </div>
    </details>
  </div>

  <!-- Scan diagnostics -->
  <div class="section">
    <details>
      <summary>Scan Diagnostics — {len(diag or [])} tickers checked</summary>
      <p style="color:#666;font-size:12px;margin:8px 0 6px">
        Shows what the scanner saw for each ticker this run. Entries only fire when status = "LONG signal" or "SHORT signal".
      </p>
      <table style="margin-top:4px">
        <thead><tr>
          <th>Ticker</th><th>Status</th><th>Close</th>
          <th>ORB High</th><th>ORB Low</th><th>ORB Width</th><th>Vol Ratio</th><th>Note</th>
        </tr></thead>
        <tbody>{_diag_rows(diag or [])}</tbody>
      </table>
    </details>
  </div>

  {build_journal_section(nav, idx_returns, spy_cum)}

  <p style="color:#bbb;font-size:11px;margin-top:16px" id="live-status">
    intraday_trader.py &nbsp;|&nbsp; Simulated only — not real money &nbsp;|&nbsp; Not financial advice.
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
    const t = new Date().toLocaleTimeString('en-US', {{timeZone:'America/New_York'}});
    const badge = document.getElementById('live-badge');
    const status = document.getElementById('live-status');
    if (badge) badge.textContent = `Updated ${{t}} ET`;
    if (status) {{
      status.innerHTML = `Live prices as of ${{t}} ET &nbsp;|&nbsp; Simulated only — not real money.`;
      status.style.color = '#388e3c';
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
    const portVal = cashVal + openValue;
    const ret     = portVal - STARTING;
    const retPct  = ret / STARTING * 100;
    const sign    = ret >= 0 ? '+' : '';
    const color   = ret >= 0 ? '#2e7d32' : '#c62828';

    const portEl  = document.getElementById('port-value');
    const totalEl = document.getElementById('port-total');
    if (portEl) portEl.textContent = '$' + portVal.toLocaleString('en-US', {{maximumFractionDigits:0}});
    if (totalEl) {{
      totalEl.innerHTML = `${{sign}}$${{Math.abs(ret).toLocaleString('en-US',{{maximumFractionDigits:0}})}}<br><span style="font-size:14px">${{sign}}${{Math.abs(retPct).toFixed(2)}}%</span>`;
      totalEl.style.color = color;
    }}
  }}

  if (isMarketHours()) {{
    fetchPrices();
    setInterval(fetchPrices, 30000);  // every 30s for intraday
  }}
}})();

// NAV chart with SPY benchmark
(function() {{
  const data = {nav_js};
  if (!data.dates.length) return;
  const canvas = document.getElementById('nav-chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 800, H = 140;
  canvas.width = W; canvas.height = H;

  const vals = data.values, spy = data.spy || [], N = vals.length;
  if (N < 1) return;
  const pad = 20;

  const allVals = [...vals, ...spy].filter(v => v != null);
  const minV = Math.min(...allVals), maxV = Math.max(...allVals);
  const range = maxV - minV || 1;

  const toX = i => pad + (i / (N - 1 || 1)) * (W - 2 * pad);
  const toY = v => H - pad - ((v - minV) / range) * (H - 2 * pad);

  // SPY line (orange dashed)
  if (spy.length) {{
    ctx.strokeStyle = '#ff8f00'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
    ctx.beginPath();
    spy.forEach((v, i) => {{ if (v != null) i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)); }});
    ctx.stroke(); ctx.setLineDash([]);
  }}

  // Portfolio line (solid orange-red)
  ctx.strokeStyle = '#e65100'; ctx.lineWidth = 2;
  ctx.beginPath();
  vals.forEach((v, i) => i === 0 ? ctx.moveTo(toX(i), toY(v)) : ctx.lineTo(toX(i), toY(v)));
  ctx.stroke();

  // Fill
  ctx.lineTo(toX(N-1), H - pad); ctx.lineTo(toX(0), H - pad); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(230,81,0,0.12)'); grad.addColorStop(1, 'rgba(230,81,0,0)');
  ctx.fillStyle = grad; ctx.fill();

  // Labels
  ctx.font = '11px sans-serif'; ctx.fillStyle = '#666';
  if (data.dates.length) {{
    ctx.fillText(data.dates[0], pad, H - 4);
    const last = data.dates[N-1];
    ctx.fillText(last, W - pad - ctx.measureText(last).width, H - 4);
  }}
  const lastVal = '$' + vals[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
  ctx.fillStyle = vals[N-1] >= {STARTING_CAPITAL} ? '#2e7d32' : '#c62828';
  ctx.font = 'bold 12px sans-serif';
  ctx.fillText(lastVal, toX(N-1) - ctx.measureText(lastVal).width - 4, toY(vals[N-1]) - 4);
  if (spy.length) {{
    const spyLast = 'SPY $' + spy[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
    ctx.fillStyle = '#ff8f00'; ctx.font = '11px sans-serif';
    ctx.fillText(spyLast, toX(N-1) - ctx.measureText(spyLast).width - 4, toY(spy[N-1]) + 14);
  }}
  // Legend
  ctx.font = '11px sans-serif';
  ctx.fillStyle = '#e65100'; ctx.fillRect(pad, 6, 18, 3);
  ctx.fillStyle = '#333'; ctx.fillText('Portfolio', pad + 22, 12);
  ctx.strokeStyle = '#ff8f00'; ctx.setLineDash([4,4]); ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(pad + 90, 8); ctx.lineTo(pad + 108, 8); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#333'; ctx.fillText('SPY (buy & hold)', pad + 112, 12);
}})();
</script>
</body>
</html>"""

    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    out = report_dir / f"intraday_{today_str}.html"
    out.write_text(html)
    print(f"  Dashboard → {out}")


if __name__ == "__main__":
    run_intraday()
