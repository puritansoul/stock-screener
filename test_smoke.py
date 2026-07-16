"""
Smoke test: import and execute every dashboard builder with minimal state.
Catches NameError, AttributeError, and other runtime failures that
py_compile misses. Run before every push: python3 test_smoke.py
"""
import sys
import traceback
import importlib
from datetime import date, timedelta
from pathlib import Path
import pandas as pd

PASS = []
FAIL = []

def check(label: str, fn):
    try:
        fn()
        PASS.append(label)
        print(f"  PASS  {label}")
    except Exception as e:
        FAIL.append(label)
        print(f"  FAIL  {label}")
        traceback.print_exc()

# ── shared fixtures ────────────────────────────────────────────────────────────
today     = date.today().isoformat()
yesterday = (date.today() - timedelta(days=1)).isoformat()

# ── swing_trader ──────────────────────────────────────────────────────────────
def _swing():
    import swing_trader as sw
    state = {
        "capital": 90_000.0,
        "open_positions": [
            {"ticker": "AAPL", "side": "long",  "entry_price": 190.0, "shares": 10,
             "stop": 185.0, "entry_date": yesterday, "cost": 1900.0, "rsi2_entry": 5.2,
             "peak": 192.0},
            {"ticker": "TSLA", "side": "short", "entry_price": 250.0, "shares": 5,
             "stop": 258.0, "entry_date": yesterday, "cost": 1250.0, "rsi2_entry": 94.1,
             "peak": 247.0},
        ],
        "closed_positions": [
            {"ticker": "MSFT", "side": "long",  "entry_price": 400.0, "exit_price": 410.0,
             "shares": 3, "pnl": 30.0, "pnl_pct": 2.5, "cost": 1200.0,
             "entry_date": yesterday, "exit_date": today, "exit_reason": "RSI exit"},
        ],
        "nav_history":  {yesterday: 99_000.0, today: 100_000.0},
        "inception_date": yesterday,
        "last_scan": [
            {"ticker": "NVDA", "status": "LONG signal", "rsi2": 4.2,
             "close": 900.0, "sma200": 750.0},
        ],
    }
    prices = pd.DataFrame(
        {"AAPL": [190.5], "TSLA": [248.0]},
        index=pd.to_datetime([today]),
    )
    sw.build_swing_dashboard(state, prices)

# ── intraday_trader ───────────────────────────────────────────────────────────
def _intraday():
    import intraday_trader as it
    state = {
        "capital": 3_000.0,
        "open_positions": [
            {"ticker": "SPY", "side": "long", "entry_price": 550.0, "shares": 10,
             "stop": 545.0, "peak": 551.0, "orb_width": 2.0, "trail_dist": 2.0,
             "vol_ratio": 1.5, "entry_time": "09:35 ET", "entry_date": today, "cost": 5500.0},
        ],
        "closed_today": [
            {"ticker": "QQQ", "side": "long", "entry_price": 480.0, "exit_price": 482.0,
             "shares": 5, "pnl": 10.0, "pnl_pct": 0.4,
             "entry_time": "09:35 ET", "exit_time": "10:15 ET",
             "entry_date": today, "exit_date": today, "exit_reason": "trailing stop"},
        ],
        "all_closed": [],
        "today": today,
        "today_orb": {"SPY": {"high": 551.0, "low": 549.0, "width": 2.0}},
        "nav_history": {yesterday: 99_000.0, today: 99_500.0},
        "inception_date": yesterday,
        "log": [],
    }
    data = {
        "SPY": {
            "bars": pd.DataFrame(
                {"Open": [550.0], "High": [551.0], "Low": [549.5], "Close": [550.8], "Volume": [1_000_000]},
                index=pd.to_datetime([today]),
            ),
            "orb": {"high": 551.0, "low": 549.0, "width": 2.0},
        }
    }
    diag = [
        {"ticker": "AMZN", "status": "inside range", "close": 195.0,
         "orb_hi": 196.0, "orb_lo": 193.0, "orb_w": 3.0,
         "vol_ratio": 1.2, "note": ""},
    ]
    it.build_intraday_dashboard(state, data, diag)

# ── live_monitor ──────────────────────────────────────────────────────────────
def _live_monitor():
    import live_monitor as lm
    # Minimal scores DataFrame with required columns
    tickers = ["AAPL", "MSFT", "NVDA"]
    scores = pd.DataFrame({
        "composite":    [0.9, 0.8, 0.7],
        "momentum":     [0.8, 0.7, 0.9],
        "value":        [0.6, 0.5, 0.4],
        "quality":      [0.9, 0.8, 0.7],
        "growth":       [0.7, 0.6, 0.8],
        "low_vol":      [0.5, 0.6, 0.4],
        "close":        [190.0, 420.0, 900.0],
        "mktcap":       [3e12, 3.1e12, 2.2e12],
        "sector":       ["Tech", "Tech", "Tech"],
        "weight":       [0.34, 0.33, 0.33],
        "weight_delta": [0.0, 0.0, 0.0],
        "piotroski":    [7, 6, 5],
        "roic":         [0.3, 0.25, 0.4],
        "fcf_ev":       [0.04, 0.03, 0.05],
        "ev_ebitda":    [15.0, 18.0, 25.0],
        "debt_ebitda":  [0.5, 0.3, 0.1],
        "revenue_growth": [0.15, 0.1, 0.2],
        "eps_growth":   [0.2, 0.15, 0.3],
        "pe_ratio":     [30.0, 35.0, 50.0],
        "rsi14":        [55.0, 48.0, 62.0],
        "passed_hard_filters": [True, True, True],
    }, index=pd.Index(tickers, name="ticker"))

    trades = {
        "entries": [{"ticker": "NVDA", "shares": 1, "price": 900.0, "weight": 0.33}],
        "exits":   [],
    }
    positions = {
        "AAPL": {"shares": 10, "price": 185.0},
        "MSFT": {"shares": 5,  "price": 410.0},
    }
    nav_history = {yesterday: 99_000.0, today: 100_000.0}

    lm.save_html_report(
        today=date.today(),
        scores=scores,
        trades=trades,
        yq=(2025, 3),
        ann_year=2024,
        n_target=3,
        is_rebalance=False,
        holdings=["AAPL", "MSFT"],
        weights={"AAPL": 0.5, "MSFT": 0.5},
        alert_tickers=["NVDA"],
        nav_history=nav_history,
        inception_date=yesterday,
        positions=positions,
        prev_prices={"AAPL": 185.0, "MSFT": 410.0},
    )

# ── run ───────────────────────────────────────────────────────────────────────
print("\nSmoke tests")
print("-" * 40)
check("swing_trader.build_swing_dashboard",       _swing)
check("intraday_trader.build_intraday_dashboard", _intraday)
check("live_monitor.save_html_report",            _live_monitor)
print("-" * 40)
print(f"{len(PASS)} passed, {len(FAIL)} failed\n")

if FAIL:
    sys.exit(1)
