"""
verify.py — Run before every push.
Generates HTML from synthetic state for all 6 bots and asserts that
every number that appears in multiple places on the same page is identical.
"""

import re
import sys
import json
import traceback
from pathlib import Path
from datetime import date, timedelta

BASE_DIR = Path(__file__).parent
PASS = "✅"
FAIL = "❌"

failures: list[str] = []


# ── HTML assertion helpers ────────────────────────────────────────────────────

def extract_id(html, elem_id):
    """Dollar amount inside the first element with id=elem_id."""
    m = re.search(rf'id="{elem_id}"[^>]*>\$?([\d,]+)', html)
    return int(m.group(1).replace(",", "")) if m else None

def extract_after(html, label):
    """Dollar amount in the <td> immediately after a <td> containing label."""
    m = re.search(rf'{re.escape(label)}</td>\s*<td[^>]*>\$?([\d,]+)', html)
    return int(m.group(1).replace(",", "")) if m else None

def extract_bold_after(html, label):
    """Dollar amount in a bold cell after a plain cell containing label."""
    m = re.search(rf'>{re.escape(label)}<.*?>\$([\d,]+)<', html, re.DOTALL)
    return int(m.group(1).replace(",", "")) if m else None

def check(bot: str, label: str, a_label: str, a_val, b_label: str, b_val):
    if a_val is None:
        failures.append(f"{bot}: {a_label} NOT FOUND in HTML")
        return
    if b_val is None:
        failures.append(f"{bot}: {b_label} NOT FOUND in HTML")
        return
    if a_val != b_val:
        failures.append(f"{bot}: {label} MISMATCH — {a_label}=${a_val:,} vs {b_label}=${b_val:,}")
    else:
        print(f"  {PASS} {label}: ${a_val:,}")


# ── Per-bot verification ──────────────────────────────────────────────────────

def verify_swing(label: str, module_name: str, html_pattern: str):
    print(f"\n[{label}]")
    try:
        import importlib
        mod = importlib.import_module(module_name)
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        import numpy as np
        import pandas as pd
        STARTING = 100_000.0
        state = {
            "capital": 72_000.0,
            "open_positions": [
                {"ticker": "AAPL", "side": "long", "entry_price": 170.0, "shares": 100,
                 "stop": 163.0, "cost": 17_000.0, "entry_date": yesterday, "rsi2_entry": 4.1},
                {"ticker": "MSFT", "side": "long", "entry_price": 380.0, "shares": 30,
                 "stop": 368.0, "cost": 11_400.0, "entry_date": yesterday, "rsi2_entry": 3.7},
            ],
            "closed_positions": [],
            "nav_history": {yesterday: 101_000.0, today: 99_500.0},  # today is a loss day — tests sign rendering
            "inception_date": yesterday,
            "scan_results": [],
            "log": [],
        }
        dates = pd.date_range(end=today, periods=220, freq="B")
        prices = pd.DataFrame(
            {"AAPL": 175.0 + np.linspace(0, 5, 220),
             "MSFT": 385.0 + np.linspace(0, 8, 220),
             "NVDA": 500.0 + np.linspace(0, 10, 220)},
            index=dates,
        )
        mod.build_swing_dashboard(state, prices)

        html_files = sorted((BASE_DIR / "reports").glob(html_pattern), reverse=True)
        if not html_files:
            failures.append(f"{label}: no report file matching {html_pattern}")
            return
        html = html_files[0].read_text()

        # Expected portfolio value = capital + sum of (cur_price * shares) for longs
        aapl_curval = float(prices["AAPL"].iloc[-1]) * 100
        msft_curval = float(prices["MSFT"].iloc[-1]) * 30
        expected_portval = round(72_000 + aapl_curval + msft_curval)

        card_val    = extract_id(html, "port-value")
        bk_val      = extract_bold_after(html, "Portfolio Value")
        stats_val   = extract_after(html, "Current Value")

        check(label, "port-value card vs Capital Breakdown",
              "#port-value card", card_val, "Capital Breakdown total", bk_val)
        check(label, "port-value card vs Strategy Stats",
              "#port-value card", card_val, "Strategy Stats Current Value", stats_val)

        # Verify formula: card value == capital + open positions current value
        if card_val is not None and abs(card_val - expected_portval) > 1:
            failures.append(
                f"{label}: portfolio value formula wrong — "
                f"card=${card_val:,} but capital+holdings=${expected_portval:,}"
            )
        else:
            print(f"  {PASS} formula: capital + holdings = ${card_val:,}")

        # Capital Breakdown: cash + invested + unrealized = portfolio value
        cash_val   = extract_bold_after(html, "Cash (idle)")
        invest_val = extract_id(html, "bk-invested")
        unreal_val_m = re.search(r'id="bk-unreal"[^>]*>[+-]?\$([\d,]+)', html)
        unreal_sign  = re.search(r'id="bk-unreal"[^>]*>([+-])', html)
        if cash_val and invest_val and unreal_val_m and card_val:
            unreal_v = int(unreal_val_m.group(1).replace(",", ""))
            sign = -1 if (unreal_sign and unreal_sign.group(1) == "-") else 1
            breakdown_sum = cash_val + invest_val + sign * unreal_v
            if abs(breakdown_sum - card_val) > 1:
                failures.append(
                    f"{label}: Capital Breakdown doesn't add up — "
                    f"cash(${cash_val:,}) + invested(${invest_val:,}) + unreal(${sign*unreal_v:,}) "
                    f"= ${breakdown_sum:,} but port-value=${card_val:,}"
                )
            else:
                print(f"  {PASS} Capital Breakdown sum = ${breakdown_sum:,}")

        # Journal sign check: negative day P&L must render with a minus sign
        # The synthetic nav_history has today < yesterday so today's row is a loss
        neg_day_m = re.search(r'journal-table.*?<tr>.*?<td[^>]*>(-\$[\d,]+|\$[\d,]+)</td>', html, re.DOTALL)
        if neg_day_m:
            cell_text = neg_day_m.group(1)
            if not cell_text.startswith("-$"):
                failures.append(f"{label}: journal negative day P&L missing minus sign — got '{cell_text}'")
            else:
                print(f"  {PASS} journal negative day P&L sign: {cell_text}")

    except Exception as e:
        failures.append(f"{label}: EXCEPTION — {e}")
        traceback.print_exc()


def verify_intraday(label: str, module_name: str, html_pattern: str):
    print(f"\n[{label}]")
    try:
        import importlib
        import io
        from contextlib import redirect_stdout
        mod = importlib.import_module(module_name)

        # Use the module's own smoke test to generate the HTML
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod._smoke_test()

        html_files = sorted((BASE_DIR / "reports").glob(html_pattern), reverse=True)
        if not html_files:
            failures.append(f"{label}: no report file matching {html_pattern}")
            return
        html = html_files[0].read_text()

        card_val = extract_id(html, "port-value")
        if card_val is None:
            failures.append(f"{label}: #port-value NOT FOUND")
            return
        print(f"  {PASS} #port-value card: ${card_val:,}")

        # port-value must equal capital (from CASH JS constant) + total-curval
        # We verify: total-curval + cash_card value == port-value
        # cash card has no id — extract from the "Cash" card-label context
        cash_m = re.search(r'card-label[^>]*>Cash</div>\s*<div[^>]*>\$([\d,]+)', html)
        total_curval = extract_id(html, "total-curval")

        if cash_m and total_curval is not None:
            cash_val = int(cash_m.group(1).replace(",", ""))
            reconstructed = cash_val + total_curval
            if abs(reconstructed - card_val) > 1:
                failures.append(
                    f"{label}: cash(${cash_val:,}) + total-curval(${total_curval:,}) "
                    f"= ${reconstructed:,} but #port-value=${card_val:,}"
                )
            else:
                print(f"  {PASS} cash + total-curval = port-value: ${reconstructed:,}")
        else:
            print(f"  (cash card or total-curval not found — skipping additive check)")

        # total-unreal-d must equal total-curval - total-cost-basis
        # total-cost-basis has no id, but we can verify via data-server-total attr
        server_total_m = re.search(r'data-server-total="([+-]?[\d.]+)"', html)
        total_unreal = extract_id(html, "total-unreal-d")
        if server_total_m and total_unreal is not None:
            server_val = round(float(server_total_m.group(1)))
            if abs(total_unreal - abs(server_val)) > 1:
                failures.append(
                    f"{label}: total-unreal-d display(${total_unreal:,}) "
                    f"!= data-server-total(${server_val:,})"
                )
            else:
                print(f"  {PASS} total-unreal-d matches server total: ${total_unreal:,}")

    except Exception as e:
        failures.append(f"{label}: EXCEPTION — {e}")
        traceback.print_exc()


def verify_screener(label: str, html_pattern: str):
    """Parse the most recent screener report from disk and check internal consistency."""
    print(f"\n[{label}]")
    try:
        html_files = sorted((BASE_DIR / "reports").glob(html_pattern), reverse=True)
        if not html_files:
            print(f"  (no report on disk matching {html_pattern} — skipping)")
            return
        html = html_files[0].read_text()
        print(f"  (using {html_files[0].name})")

        card_val = extract_id(html, "port-value")
        if card_val is None:
            failures.append(f"{label}: #port-value NOT FOUND")
            return
        print(f"  {PASS} #port-value card: ${card_val:,}")

        # NAV badge in header must match card
        badge_m = re.search(r'NAV: \$([\d,]+)', html)
        if badge_m:
            badge_val = int(badge_m.group(1).replace(",", ""))
            check(label, "port-value card vs NAV badge",
                  "#port-value card", card_val, "NAV badge", badge_val)
        else:
            print(f"  (no NAV badge found — skipping badge check)")

        # port-today and port-total should be present and non-zero when holdings exist
        today_el = re.search(r'id="port-today"[^>]*>([^<]+)', html)
        total_el = re.search(r'id="port-total"[^>]*>([^<]+)', html)
        if today_el:
            print(f"  {PASS} port-today present: {today_el.group(1).strip()}")
        else:
            failures.append(f"{label}: #port-today NOT FOUND")
        if total_el:
            print(f"  {PASS} port-total present: {total_el.group(1).strip()}")
        else:
            failures.append(f"{label}: #port-total NOT FOUND")

        # Holdings totals row: tot-invested, tot-day-d, tot-ret-d should all be present
        for eid in ("tot-invested", "tot-day-d", "tot-ret-d"):
            if f'id="{eid}"' in html:
                print(f"  {PASS} #{eid} present")
            else:
                failures.append(f"{label}: #{eid} NOT FOUND in holdings totals row")

    except Exception as e:
        failures.append(f"{label}: EXCEPTION — {e}")
        traceback.print_exc()


def verify_summary_bar():
    """Summary bar static values must match each bot's latest report #port-value."""
    print(f"\n[Summary Bar vs Reports]")
    try:
        import importlib
        mod = importlib.import_module("build_consolidated")
        importlib.reload(mod)

        bots = [
            ("Swing v1",    BASE_DIR / "swing_trades.json",       "swing_[0-9]*.html"),
            ("Intraday v1", BASE_DIR / "intraday_trades.json",    "intraday_[0-9]*.html"),
            ("Swing v2",    BASE_DIR / "swing_trades_v2.json",    "swing_v2_*.html"),
            ("Intraday v2", BASE_DIR / "intraday_trades_v2.json", "intraday_v2_*.html"),
        ]
        for name, state_path, glob in bots:
            files = sorted((BASE_DIR / "reports").glob(glob), reverse=True)
            if not files:
                print(f"  (no report for {name} — skipping)")
                continue
            html = files[0].read_text()
            report_val = extract_id(html, "port-value")

            state = mod._load_json(state_path) if state_path.exists() else {}
            bar_cur, _ = mod._nav_value(state.get("nav_history", {}), mod.STARTING_CAPITAL, glob)
            bar_val = round(bar_cur)

            check("Summary Bar", f"{name} bar vs report",
                  f"{name} bar", bar_val, f"{name} report", report_val)
    except Exception as e:
        failures.append(f"Summary Bar: EXCEPTION — {e}")
        traceback.print_exc()


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    os.chdir(BASE_DIR)
    sys.path.insert(0, str(BASE_DIR))

    print("=" * 60)
    print("verify.py — cross-section consistency checks for all 6 bots")
    print("=" * 60)

    verify_swing("Swing Trader v1",    "swing_trader",    "swing_[0-9]*.html")
    verify_swing("Swing Trader v2",    "swing_trader_v2", "swing_v2_*.html")
    verify_intraday("Intraday Trader v1", "intraday_trader",    "intraday_[0-9]*.html")
    verify_intraday("Intraday Trader v2", "intraday_trader_v2", "intraday_v2_*.html")
    verify_screener("Factor Screener v1", "[0-9]*-[0-9]*-[0-9]*.html")
    verify_screener("Factor Screener v2", "v2_*.html")
    verify_summary_bar()

    print("\n" + "=" * 60)
    if failures:
        print(f"{FAIL} {len(failures)} failure(s):")
        for f in failures:
            print(f"  {FAIL} {f}")
        sys.exit(1)
    else:
        print(f"{PASS} All checks passed — safe to push")
