"""
Build overview.html — master trading dashboard, first tab in consolidated view.
Pure Python + inline SVG/CSS, no external dependencies.
"""

from pathlib import Path
from datetime import date, timedelta
import json, math

BASE_DIR   = Path(__file__).parent
STARTING   = 100_000.0
TODAY      = date.today().isoformat()


# ── helpers ──────────────────────────────────────────────────────────────────

def _load(name: str):
    p = BASE_DIR / name
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _pct_color(pct: float) -> str:
    """Green→red gradient for a % return."""
    if pct >= 2:    return "#1b5e20"
    if pct >= 1:    return "#2e7d32"
    if pct >= 0.3:  return "#388e3c"
    if pct >= 0:    return "#43a047"
    if pct >= -0.3: return "#e53935"
    if pct >= -1:   return "#c62828"
    if pct >= -2:   return "#b71c1c"
    return "#7f0000"


def _sign(n: float) -> str:
    return "+" if n >= 0 else "-"


def _fmt(n: float) -> str:
    return f"{_sign(n)}${abs(n):,.0f}"


def _fmt_pct(p: float) -> str:
    return f"{_sign(p)}{abs(p):.2f}%"


def _nav_series(raw, starting: float):
    """Return {date_str: dollar_value} from any nav structure."""
    if isinstance(raw, dict):
        # Could be {date: nav_unit} or {date: dollar}
        items = sorted(raw.items())
        if not items:
            return {}
        first_val = items[0][1]
        if first_val < 100:          # NAV units (e.g. 1.0 = 100k)
            return {d: v * starting for d, v in items}
        return {d: v for d, v in items}
    return {}


# ── data loading ─────────────────────────────────────────────────────────────

def load_bots():
    """Return list of bot dicts with all data needed for the dashboard."""
    import importlib
    bc = importlib.import_module("build_consolidated")
    importlib.reload(bc)

    bots = []

    # cur + day_pnl come from the same HTML reports the horizontal bar uses —
    # data-pnl attribute, no text parsing, always in sync with each dashboard.
    configs = [
        # (label, color, nav_file, report_glob, state_file, closed_key)
        # Screeners: nav_file is portfolio_nav.json (always current, updated each run)
        # Traders:   report_glob reads data-pnl from HTML (written each run)
        ("Screener v1", "#1565c0", BASE_DIR/"portfolio_nav.json",    "",                    "portfolio_nav.json",     None),
        ("Swing v1",    "#6a1b9a", None,                             "swing_[0-9]*.html",   "swing_trades.json",      "closed_positions"),
        ("Intraday v1", "#00695c", None,                             "intraday_[0-9]*.html","intraday_trades.json",   "all_closed"),
        ("Screener v2", "#0277bd", BASE_DIR/"portfolio_nav_v2.json", "",                    "portfolio_nav_v2.json",  None),
        ("Swing v2",    "#ad1457", None,                             "swing_v2_*.html",     "swing_trades_v2.json",   "closed_positions"),
        ("Intraday v2", "#00838f", None,                             "intraday_v2_*.html",  "intraday_trades_v2.json","all_closed"),
    ]

    for label, color, nav_file, report_glob, state_file, closed_key in configs:
        # cur + day_pnl: screeners from portfolio_nav.json (always fresh),
        # traders from HTML report data-pnl attribute
        if nav_file:
            cur, day_pnl = bc._screener_nav(nav_file, STARTING)
        else:
            cur, day_pnl = bc._report_values(report_glob, STARTING)

        # nav_history for sparklines and equity curve
        raw = _load(state_file)
        if nav_file:
            nav = _nav_series(raw, STARTING)        # portfolio_nav.json: {date: nav_unit}
        else:
            nav = _nav_series(raw.get("nav_history", {}), STARTING) if isinstance(raw, dict) else {}

        # Patch today's nav point only if cur is plausible (within 30% of last known nav).
        # Screener reports can be stale mid-day, which would corrupt the equity curve.
        if nav:
            last_nav = nav[max(nav.keys())]
            if abs(cur - last_nav) / last_nav < 0.30:
                nav[TODAY] = cur

        closed = []
        if closed_key and isinstance(raw, dict):
            closed = raw.get(closed_key, [])

        peak      = max(nav.values()) if nav else STARTING
        prev      = cur - day_pnl
        day_pct   = day_pnl / prev * 100 if prev else 0
        total_ret = (cur - STARTING) / STARTING * 100

        # per-trade stats from closed trades
        wins   = [t["pnl"] for t in closed if t.get("pnl", 0) > 0]
        losses = [t["pnl"] for t in closed if t.get("pnl", 0) <= 0]
        total_trades = len(closed)
        win_rate     = len(wins) / total_trades * 100 if total_trades else 0
        avg_win      = sum(wins)  / len(wins)   if wins   else 0
        avg_loss     = sum(losses)/ len(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        expectancy    = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
        drawdown_pct  = (cur - peak) / peak * 100 if peak else 0

        bots.append({
            "label":         label,
            "color":         color,
            "cur":           cur,
            "prev":          prev,
            "day_pnl":       day_pnl,
            "day_pct":       day_pct,
            "total_ret":     total_ret,
            "peak":          peak,
            "drawdown_pct":  drawdown_pct,
            "nav":           nav,
            "closed":        closed,
            "win_rate":      win_rate,
            "avg_win":       avg_win,
            "avg_loss":      avg_loss,
            "profit_factor": profit_factor,
            "expectancy":    expectancy,
            "total_trades":  total_trades,
        })

    return bots


# ── section builders ──────────────────────────────────────────────────────────

def section_bot_cards(bots: list[dict]) -> str:
    cards = ""
    for b in bots:
        dc  = "#2e7d32" if b["day_pnl"] >= 0 else "#c62828"
        rc  = "#2e7d32" if b["total_ret"] >= 0 else "#c62828"
        ddc = "#c62828" if b["drawdown_pct"] < -1 else ("#f57f17" if b["drawdown_pct"] < 0 else "#2e7d32")
        dd  = f'{b["drawdown_pct"]:.1f}%'

        # mini sparkline — last 14 nav points
        nav_dates = sorted(b["nav"].keys())[-14:]
        vals = [b["nav"][d] for d in nav_dates]
        spark = _sparkline(vals, 80, 28, b["color"])

        cards += f"""
        <div class="bot-card">
          <div class="bot-card-header">
            <span class="bot-label" style="color:{b['color']}">{b['label']}</span>
            <span class="bot-dd" style="color:{ddc}">DD {dd}</span>
          </div>
          <div class="bot-value">${b['cur']:,.0f}</div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px">
            <div>
              <span class="bot-day" style="color:{dc}">{_sign(b['day_pnl'])}${abs(b['day_pnl']):,.0f} today</span>
              <span class="bot-pct" style="color:{dc}">({_fmt_pct(b['day_pct'])})</span>
            </div>
            <span class="bot-ret" style="color:{rc}">{_fmt_pct(b['total_ret'])} all-time</span>
          </div>
          <div class="spark">{spark}</div>
        </div>"""
    return f'<div class="bot-cards-grid">{cards}</div>'


def _sparkline(vals: list[float], w: int, h: int, color: str) -> str:
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1
    pts = []
    for i, v in enumerate(vals):
        x = i / (len(vals) - 1) * w
        y = h - ((v - lo) / rng) * (h - 4) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    path = " ".join(pts)
    # area fill
    area = f"0,{h} {path} {w},{h}"
    c_dim = color + "30"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polygon points="{area}" fill="{c_dim}"/>'
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.5"/>'
            f'</svg>')


def section_heatmap(bots: list[dict]) -> str:
    cells = ""
    for b in bots:
        bg  = _pct_color(b["day_pct"])
        txt = "#fff"
        cells += f"""
        <div class="hm-cell" style="background:{bg}">
          <div class="hm-name">{b['label']}</div>
          <div class="hm-pct">{_fmt_pct(b['day_pct'])}</div>
          <div class="hm-abs">{_sign(b['day_pnl'])}${abs(b['day_pnl']):,.0f}</div>
        </div>"""
    return f'<div class="section-title">Today\'s Performance</div><div class="heatmap-grid">{cells}</div>'


def section_equity_curve(bots: list[dict]) -> str:
    """SVG overlay of all 6 NAV histories, normalized to $100K start."""
    W, H, PAD_L, PAD_B, PAD_T, PAD_R = 900, 220, 52, 36, 16, 12

    # Union of all dates
    all_dates = sorted({d for b in bots for d in b["nav"].keys()})
    if len(all_dates) < 2:
        return ""

    # Value range
    all_vals = [v for b in bots for v in b["nav"].values()]
    lo = min(all_vals) * 0.999
    hi = max(all_vals) * 1.001
    rng = hi - lo or 1

    def xp(i):  return PAD_L + i / (len(all_dates) - 1) * (W - PAD_L - PAD_R)
    def yp(v):  return PAD_T + (1 - (v - lo) / rng) * (H - PAD_T - PAD_B)

    # Y grid lines (4 levels)
    grid_svg = ""
    for level in [lo, lo + rng*0.33, lo + rng*0.66, hi]:
        y = yp(level)
        lbl = f"${level/1000:.0f}k"
        grid_svg += (f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
                     f'stroke="#e0e0e0" stroke-width="0.5"/>'
                     f'<text x="{PAD_L-4}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="#888">{lbl}</text>')

    # X axis labels — show ~6 dates
    step = max(1, len(all_dates) // 6)
    for i in range(0, len(all_dates), step):
        x = xp(i)
        lbl = all_dates[i][5:]  # MM-DD
        grid_svg += (f'<text x="{x:.1f}" y="{H-PAD_B+14}" text-anchor="middle" '
                     f'font-size="10" fill="#888">{lbl}</text>')

    # $100K baseline
    y100 = yp(STARTING)
    grid_svg += (f'<line x1="{PAD_L}" y1="{y100:.1f}" x2="{W-PAD_R}" y2="{y100:.1f}" '
                 f'stroke="#bdbdbd" stroke-width="1" stroke-dasharray="4,3"/>')

    # Bot lines
    lines_svg = ""
    for b in bots:
        pts = []
        for i, d in enumerate(all_dates):
            if d in b["nav"]:
                pts.append((xp(i), yp(b["nav"][d])))
        if len(pts) < 2:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        lines_svg += (f'<polyline points="{path}" fill="none" stroke="{b["color"]}" '
                      f'stroke-width="2" stroke-linejoin="round"/>')
        # end dot
        ex, ey = pts[-1]
        lines_svg += f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3" fill="{b["color"]}"/>'

    # Legend
    legend_svg = ""
    for i, b in enumerate(bots):
        lx = PAD_L + i * 140
        legend_svg += (f'<rect x="{lx}" y="4" width="14" height="3" fill="{b["color"]}" rx="1"/>'
                       f'<text x="{lx+18}" y="11" font-size="10" fill="#555">{b["label"]}</text>')

    svg = (f'<svg width="100%" viewBox="0 0 {W} {H+20}" '
           f'style="display:block;max-width:{W}px">'
           f'{legend_svg}{grid_svg}{lines_svg}</svg>')

    return f'<div class="section-title">Equity Curves</div><div class="chart-wrap">{svg}</div>'


def section_calendar(bots: list[dict]) -> str:
    """60-day combined P&L calendar heatmap."""
    # Build daily combined P&L from all bots' nav histories
    daily = {}
    for b in bots:
        dates = sorted(b["nav"].keys())
        for i in range(1, len(dates)):
            d    = dates[i]
            dpnl = b["nav"][dates[i]] - b["nav"][dates[i-1]]
            daily[d] = daily.get(d, 0) + dpnl

    if not daily:
        return ""

    # Last 60 calendar days
    end   = date.fromisoformat(TODAY)
    start = end - timedelta(days=59)

    # Build week columns
    # Find first Monday on or before start
    first_monday = start - timedelta(days=start.weekday())
    weeks = []
    cur = first_monday
    while cur <= end:
        week = []
        for wd in range(7):
            d = cur + timedelta(days=wd)
            week.append(d)
        weeks.append(week)
        cur += timedelta(days=7)

    max_abs = max((abs(v) for v in daily.values()), default=1) or 1

    cells = ""
    day_labels = ["M", "T", "W", "T", "F", "S", "S"]
    # day-of-week labels
    dow_col = ""
    for i, lbl in enumerate(day_labels):
        dow_col += f'<div class="cal-dow">{lbl}</div>'

    col_html = ""
    month_labels = ""
    prev_month = None
    for wi, week in enumerate(weeks):
        col = ""
        for day in week:
            ds = day.isoformat()
            if day < start or day > end:
                col += '<div class="cal-cell cal-empty"></div>'
                continue
            pnl = daily.get(ds, None)
            if pnl is None:
                col += '<div class="cal-cell cal-nodata" title="No data"></div>'
                continue
            intensity = min(abs(pnl) / max_abs, 1.0)
            if pnl >= 0:
                r = int(200 - 150 * intensity)
                g = int(200 + 55 * intensity)
                b = int(200 - 150 * intensity)
            else:
                r = int(200 + 55 * intensity)
                g = int(200 - 150 * intensity)
                b = int(200 - 150 * intensity)
            bg = f"rgb({r},{g},{b})"
            tip = f"{ds}: {_sign(pnl)}${abs(pnl):,.0f}"
            col += f'<div class="cal-cell" style="background:{bg}" title="{tip}"></div>'

        # month label above first week of each month
        first_real = next((d for d in week if start <= d <= end), None)
        mlbl = ""
        if first_real:
            m = first_real.strftime("%b")
            if m != prev_month:
                prev_month = m
                mlbl = m
        col_html += f'<div class="cal-col"><div class="cal-month-lbl">{mlbl}</div>{col}</div>'

    html = f"""
    <div class="section-title">60-Day P&amp;L Calendar <span style="font-size:11px;font-weight:normal;color:#888">(combined all bots)</span></div>
    <div class="cal-wrap">
      <div class="cal-dow-col">{dow_col}</div>
      <div class="cal-cols">{col_html}</div>
      <div class="cal-legend">
        <span style="color:#888;font-size:11px">Loss</span>
        <div class="cal-legend-bar"></div>
        <span style="color:#888;font-size:11px">Gain</span>
      </div>
    </div>"""
    return html


def section_gainers_losers(bots: list[dict]) -> str:
    """Top 3 / bottom 3 recent closed trades across all bots."""
    all_closed = []
    for b in bots:
        for t in b["closed"]:
            all_closed.append({**t, "_bot": b["label"], "_color": b["color"]})

    if not all_closed:
        return ""

    # Sort by pnl
    by_pnl = sorted(all_closed, key=lambda t: t.get("pnl", 0), reverse=True)
    gainers = by_pnl[:3]
    losers  = by_pnl[-3:][::-1]

    def trade_row(t, is_gain: bool) -> str:
        pnl  = t.get("pnl", 0)
        clr  = "#2e7d32" if pnl >= 0 else "#c62828"
        bg   = "#f1f8e9" if is_gain else "#fce4ec"
        return (f'<tr style="background:{bg}">'
                f'<td><strong>{t["ticker"]}</strong></td>'
                f'<td style="color:{t["_color"]};font-size:11px">{t["_bot"]}</td>'
                f'<td>{t.get("exit_date","—")}</td>'
                f'<td style="color:{clr};font-weight:bold">{_sign(pnl)}${abs(pnl):,.0f}</td>'
                f'<td style="font-size:11px;color:#666">{t.get("exit_reason","—")}</td>'
                f'</tr>')

    rows_g = "".join(trade_row(t, True)  for t in gainers)
    rows_l = "".join(trade_row(t, False) for t in losers)

    table_hdr = ("<thead><tr>"
                 "<th>Ticker</th><th>Bot</th><th>Closed</th>"
                 "<th>P&amp;L</th><th>Reason</th>"
                 "</tr></thead>")

    return f"""
    <div class="gl-wrap">
      <div class="gl-half">
        <div class="section-title" style="color:#2e7d32">Top Trades (All-time)</div>
        <table class="gl-table">{table_hdr}<tbody>{rows_g}</tbody></table>
      </div>
      <div class="gl-half">
        <div class="section-title" style="color:#c62828">Worst Trades (All-time)</div>
        <table class="gl-table">{table_hdr}<tbody>{rows_l}</tbody></table>
      </div>
    </div>"""


def section_recent_trades(bots: list[dict]) -> str:
    """Last 10 closed trades across all bots."""
    all_closed = []
    for b in bots:
        for t in b["closed"]:
            all_closed.append({**t, "_bot": b["label"], "_color": b["color"]})

    if not all_closed:
        return ""

    # Sort by exit_date desc, take last 10
    recent = sorted(all_closed, key=lambda t: (t.get("exit_date",""), t.get("exit_time","")), reverse=True)[:10]

    rows = ""
    for t in recent:
        pnl = t.get("pnl", 0)
        clr = "#2e7d32" if pnl >= 0 else "#c62828"
        entry = t.get("entry_date", "—")
        ex    = t.get("exit_date", "—")
        # hold time
        try:
            hold = (date.fromisoformat(ex) - date.fromisoformat(entry)).days
            hold_str = f"{hold}d"
        except Exception:
            hold_str = "—"
        rows += (f'<tr>'
                 f'<td><strong>{t["ticker"]}</strong></td>'
                 f'<td style="color:{t["_color"]};font-size:11px">{t["_bot"]}</td>'
                 f'<td style="font-size:11px">{entry}</td>'
                 f'<td style="font-size:11px">{ex}</td>'
                 f'<td style="color:#666;font-size:11px">{hold_str}</td>'
                 f'<td style="color:{clr};font-weight:bold">{_sign(pnl)}${abs(pnl):,.0f}</td>'
                 f'<td style="font-size:11px;color:#666">{t.get("exit_reason","—")}</td>'
                 f'</tr>')

    return f"""
    <div class="section-title">Recent Closed Trades</div>
    <table class="data-table">
      <thead><tr>
        <th>Ticker</th><th>Bot</th><th>Entry</th><th>Exit</th>
        <th>Hold</th><th>P&amp;L</th><th>Reason</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def section_scorecard(bots: list[dict]) -> str:
    """Per-bot stats: win rate, avg win/loss, profit factor, expectancy, trades."""
    rows = ""
    for b in bots:
        pf   = b["profit_factor"]
        pf_s = f"{pf:.2f}" if pf != float('inf') else "∞"
        exc  = b["expectancy"]
        ec   = "#2e7d32" if exc > 0 else "#c62828"
        pfc  = "#2e7d32" if pf > 1  else "#c62828"
        wrc  = "#2e7d32" if b["win_rate"] >= 50 else "#f57f17"
        rows += (f'<tr>'
                 f'<td style="color:{b["color"]};font-weight:600">{b["label"]}</td>'
                 f'<td>{b["total_trades"]}</td>'
                 f'<td style="color:{wrc}">{b["win_rate"]:.1f}%</td>'
                 f'<td style="color:#2e7d32">{_sign(b["avg_win"])}${abs(b["avg_win"]):,.0f}</td>'
                 f'<td style="color:#c62828">{_sign(b["avg_loss"])}${abs(b["avg_loss"]):,.0f}</td>'
                 f'<td style="color:{pfc}">{pf_s}</td>'
                 f'<td style="color:{ec};font-weight:bold">{_sign(exc)}${abs(exc):,.2f}</td>'
                 f'</tr>')

    return f"""
    <div class="section-title">Strategy Scorecard</div>
    <table class="data-table">
      <thead><tr>
        <th>Bot</th><th>Trades</th><th>Win Rate</th>
        <th>Avg Win</th><th>Avg Loss</th>
        <th title="Gross profit / gross loss">Profit Factor</th>
        <th title="Expected $ per trade">Expectancy</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def section_drawdown(bots: list[dict]) -> str:
    """Drawdown bars — horizontal meter per bot."""
    bars = ""
    for b in bots:
        dd   = b["drawdown_pct"]
        peak = b["peak"]
        clr  = _pct_color(dd)
        fill = min(abs(dd) / 20 * 100, 100)  # 20% = full bar
        bars += f"""
        <div class="dd-row">
          <span class="dd-label" style="color:{b['color']}">{b['label']}</span>
          <div class="dd-track">
            <div class="dd-fill" style="width:{fill:.1f}%;background:{clr}"></div>
          </div>
          <span class="dd-val" style="color:{clr}">{dd:.1f}%</span>
          <span class="dd-peak">peak ${peak:,.0f}</span>
        </div>"""
    return f'<div class="section-title">Drawdown from Peak</div><div class="dd-wrap">{bars}</div>'


# ── page builder ──────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f5f6fa; color: #212121; font-size: 13px; }
.page { max-width: 1200px; margin: 0 auto; padding: 16px; }

/* header */
.header { display:flex; align-items:center; justify-content:space-between;
          margin-bottom: 16px; }
.header h1 { font-size: 20px; font-weight: 700; color: #1a237e; }
.header .date { font-size: 12px; color: #888; }

/* total bar */
.total-bar { background:#1a237e; color:white; border-radius:10px;
             padding:14px 20px; display:flex; gap:32px; align-items:center;
             margin-bottom:16px; flex-wrap:wrap; }
.total-bar .tb-label { font-size:11px; color:rgba(255,255,255,.55);
                        text-transform:uppercase; letter-spacing:.5px; }
.total-bar .tb-val   { font-size:22px; font-weight:700; }
.total-bar .tb-item  { display:flex; flex-direction:column; gap:2px; }

/* bot cards */
.bot-cards-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px;
                  margin-bottom:16px; }
@media(max-width:700px){ .bot-cards-grid { grid-template-columns:repeat(2,1fr); } }
.bot-card { background:white; border-radius:10px; padding:12px 14px;
            box-shadow:0 1px 4px rgba(0,0,0,.08); }
.bot-card-header { display:flex; justify-content:space-between; margin-bottom:4px; }
.bot-label { font-weight:700; font-size:12px; }
.bot-dd    { font-size:11px; }
.bot-value { font-size:20px; font-weight:700; color:#212121; }
.bot-day   { font-size:12px; font-weight:600; }
.bot-pct   { font-size:11px; margin-left:2px; }
.bot-ret   { font-size:11px; }
.spark     { margin-top:6px; }

/* section titles */
.section-title { font-size:13px; font-weight:700; color:#37474f;
                 text-transform:uppercase; letter-spacing:.5px;
                 margin-bottom:8px; margin-top:4px; }

/* heatmap */
.heatmap-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:8px;
                margin-bottom:16px; }
@media(max-width:700px){ .heatmap-grid { grid-template-columns:repeat(3,1fr); } }
.hm-cell  { border-radius:8px; padding:10px 12px; color:white;
             box-shadow:0 1px 3px rgba(0,0,0,.15); }
.hm-name  { font-size:11px; font-weight:600; opacity:.85; }
.hm-pct   { font-size:16px; font-weight:700; margin:2px 0; }
.hm-abs   { font-size:11px; opacity:.85; }

/* equity curve */
.chart-wrap { background:white; border-radius:10px; padding:12px 16px;
              box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px;
              overflow-x:auto; }

/* calendar */
.cal-wrap { background:white; border-radius:10px; padding:12px 16px;
            box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px;
            display:flex; align-items:flex-start; gap:4px; overflow-x:auto; }
.cal-dow-col { display:flex; flex-direction:column; gap:2px; padding-top:20px; }
.cal-dow  { height:12px; width:14px; font-size:9px; color:#aaa; line-height:12px;
            text-align:center; }
.cal-cols { display:flex; gap:2px; }
.cal-col  { display:flex; flex-direction:column; gap:2px; }
.cal-month-lbl { height:16px; font-size:10px; color:#888; white-space:nowrap; }
.cal-cell { width:12px; height:12px; border-radius:2px; cursor:default; }
.cal-empty  { background:transparent; }
.cal-nodata { background:#eeeeee; }
.cal-legend { display:flex; flex-direction:column; align-items:center;
              gap:4px; padding-top:20px; margin-left:8px; }
.cal-legend-bar { width:10px; height:60px;
  background:linear-gradient(to bottom, #e53935, #eeeeee 50%, #43a047); border-radius:4px; }

/* drawdown */
.dd-wrap { background:white; border-radius:10px; padding:12px 16px;
           box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
.dd-row   { display:flex; align-items:center; gap:10px; padding:5px 0;
             border-bottom:1px solid #f5f5f5; }
.dd-row:last-child { border-bottom:none; }
.dd-label { width:90px; font-weight:600; font-size:12px; flex-shrink:0; }
.dd-track { flex:1; height:10px; background:#f5f5f5; border-radius:5px; overflow:hidden; }
.dd-fill  { height:100%; border-radius:5px; transition:width .3s; }
.dd-val   { width:48px; text-align:right; font-weight:700; font-size:12px; flex-shrink:0; }
.dd-peak  { font-size:11px; color:#999; flex-shrink:0; }

/* scorecard + tables */
.data-table { width:100%; border-collapse:collapse; font-size:12px; }
.data-table th { background:#f5f5f5; padding:6px 10px; text-align:left;
                 font-weight:600; color:#555; border-bottom:2px solid #e0e0e0; }
.data-table td { padding:6px 10px; border-bottom:1px solid #f0f0f0; }
.data-table tr:hover td { background:#fafafa; }
.section-wrap { background:white; border-radius:10px; padding:14px 16px;
                box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px;
                overflow-x:auto; }

/* gainers/losers */
.gl-wrap  { display:grid; grid-template-columns:1fr 1fr; gap:12px;
             margin-bottom:16px; }
@media(max-width:600px){ .gl-wrap { grid-template-columns:1fr; } }
.gl-half  { background:white; border-radius:10px; padding:14px 16px;
             box-shadow:0 1px 4px rgba(0,0,0,.08); overflow-x:auto; }
.gl-table { width:100%; border-collapse:collapse; font-size:12px; }
.gl-table th { background:#f9f9f9; padding:5px 8px; font-weight:600;
               color:#555; border-bottom:2px solid #e0e0e0; text-align:left; }
.gl-table td { padding:5px 8px; border-bottom:1px solid #f0f0f0; }

/* two-column layout */
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:16px; }
@media(max-width:700px){ .two-col { grid-template-columns:1fr; } }
"""


def build_overview():
    bots = load_bots()

    total_val     = sum(b["cur"]     for b in bots)
    total_day     = sum(b["day_pnl"] for b in bots)
    total_prev    = total_val - total_day
    total_day_pct = total_day / total_prev * 100 if total_prev else 0
    total_ret     = total_val - STARTING * len(bots)
    total_ret_pct = total_ret / (STARTING * len(bots)) * 100
    tdc = "#90caf9" if total_day >= 0 else "#ef9a9a"
    trc = "#90caf9" if total_ret >= 0 else "#ef9a9a"

    today_fmt = date.fromisoformat(TODAY).strftime("%B %-d, %Y")

    # Sorterd gainers / losers by day_pct for the total bar quick view
    sorted_day = sorted(bots, key=lambda b: b["day_pct"], reverse=True)
    best  = sorted_day[0]
    worst = sorted_day[-1]

    total_bar = f"""
    <div class="total-bar">
      <div class="tb-item">
        <span class="tb-label">Portfolio</span>
        <span class="tb-val">${total_val:,.0f}</span>
      </div>
      <div class="tb-item">
        <span class="tb-label">Today</span>
        <span class="tb-val" style="color:{tdc}">{_sign(total_day)}${abs(total_day):,.0f}
          <span style="font-size:14px">({_sign(total_day_pct)}{abs(total_day_pct):.2f}%)</span>
        </span>
      </div>
      <div class="tb-item">
        <span class="tb-label">All-time</span>
        <span class="tb-val" style="color:{trc}">{_sign(total_ret)}${abs(total_ret):,.0f}
          <span style="font-size:14px">({_fmt_pct(total_ret_pct)})</span>
        </span>
      </div>
      <div class="tb-item" style="margin-left:auto">
        <span class="tb-label">Best today</span>
        <span style="font-size:14px;font-weight:600;color:#a5d6a7">{best['label']} {_fmt_pct(best['day_pct'])}</span>
      </div>
      <div class="tb-item">
        <span class="tb-label">Worst today</span>
        <span style="font-size:14px;font-weight:600;color:#ef9a9a">{worst['label']} {_fmt_pct(worst['day_pct'])}</span>
      </div>
    </div>"""

    cards     = section_bot_cards(bots)
    equity    = section_equity_curve(bots)
    calendar  = section_calendar(bots)
    gl        = section_gainers_losers(bots)
    recent    = section_recent_trades(bots)
    scorecard = section_scorecard(bots)
    drawdown  = section_drawdown(bots)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Overview — {TODAY}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="page">
  <div class="header">
    <h1>Trading Overview</h1>
    <span class="date">{today_fmt}</span>
  </div>

  {total_bar}
  {cards}
  <div class="section-wrap">{drawdown}</div>
  {equity}
  {calendar}

  {gl}
  <div class="section-wrap">{scorecard}</div>
  <div class="section-wrap">{recent}</div>
</div>
</body>
</html>"""

    out = BASE_DIR / "overview.html"
    out.write_text(html)
    print(f"Overview → {out}")


if __name__ == "__main__":
    build_overview()
