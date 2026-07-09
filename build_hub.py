"""
Build trading_hub.html — landing page comparing swing bot vs intraday bot.
Called by both paper_trading.yml and intraday_trading.yml after each run.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

BASE_DIR        = Path(__file__).parent
SWING_FILE      = BASE_DIR / "paper_trades.json"
INTRADAY_FILE   = BASE_DIR / "intraday_trades.json"
STARTING_CAPITAL = 100_000.0


def _load(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _nav_chart_js(var: str, nav: dict, color: str) -> str:
    dates  = sorted(nav.keys())
    values = [nav[d] for d in dates]
    return f"""
(function() {{
  const dates = {json.dumps(dates)};
  const vals  = {json.dumps(values)};
  const N = vals.length;
  if (N < 2) return;
  const canvas = document.getElementById('{var}');
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 460, H = 90;
  canvas.width = W; canvas.height = H;
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const range = maxV - minV || 1, pad = 10;
  ctx.strokeStyle = '{color}'; ctx.lineWidth = 2;
  ctx.beginPath();
  vals.forEach((v, i) => {{
    const x = pad + (i / (N-1)) * (W - 2*pad);
    const y = H - pad - ((v - minV) / range) * (H - 2*pad);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.lineTo(pad + (W-2*pad), H-pad); ctx.lineTo(pad, H-pad); ctx.closePath();
  const g = ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0, '{color}28'); g.addColorStop(1, '{color}00');
  ctx.fillStyle = g; ctx.fill();
  const lastVal = '$' + vals[N-1].toLocaleString('en-US', {{maximumFractionDigits:0}});
  ctx.fillStyle = vals[N-1] >= {STARTING_CAPITAL} ? '#2e7d32' : '#c62828';
  ctx.font = 'bold 12px sans-serif';
  ctx.fillText(lastVal, W - pad - ctx.measureText(lastVal).width, 16);
  if (dates.length) {{
    ctx.fillStyle = '#999'; ctx.font = '10px sans-serif';
    ctx.fillText(dates[0], pad, H-2);
    ctx.fillText(dates[N-1], W-pad-ctx.measureText(dates[N-1]).width, H-2);
  }}
}})();"""


def _bot_card(name: str, emoji: str, color: str, chart_id: str,
              state: dict | None, today_report: str, index_file: str) -> str:
    if state is None:
        return f"""
        <div class="bot-card" style="border-top:4px solid {color}">
          <div class="bot-title">{emoji} {name}</div>
          <div style="color:#999;font-size:13px;padding:20px 0">Not started yet</div>
          <a href="{index_file}" class="btn" style="background:{color}">Open Dashboard →</a>
        </div>"""

    today_str  = date.today().isoformat()
    nav        = state.get("nav_history", {})
    capital    = state.get("capital", STARTING_CAPITAL)
    open_pos   = state.get("open_positions", [])
    closed     = state.get("all_closed", state.get("closed_positions", []))
    closed_today = state.get("closed_today", [])

    port_val   = nav.get(today_str, STARTING_CAPITAL)
    total_ret  = port_val - STARTING_CAPITAL
    total_pct  = total_ret / STARTING_CAPITAL * 100
    gc         = "#2e7d32" if total_ret >= 0 else "#c62828"
    gs         = "+" if total_ret >= 0 else ""

    all_c = closed + closed_today
    wins  = [p for p in all_c if p.get("pnl", 0) > 0]
    wr    = len(wins) / len(all_c) * 100 if all_c else 0
    day_pnl = sum(p.get("pnl", 0) for p in closed_today)
    dc = "#2e7d32" if day_pnl >= 0 else "#c62828"

    inception = state.get("inception_date", "—")
    last_run  = state.get("log", [{}])[-1].get("date", "—") if state.get("log") else "—"

    chart_section = f'<canvas id="{chart_id}" style="width:100%;margin:12px 0"></canvas>' if nav else ""

    return f"""
        <div class="bot-card" style="border-top:4px solid {color}">
          <div class="bot-title">{emoji} {name}</div>
          <div class="stat-grid">
            <div class="stat">
              <div class="stat-label">Portfolio Value</div>
              <div class="stat-val" style="color:#1a237e">${port_val:,.0f}</div>
            </div>
            <div class="stat">
              <div class="stat-label">Total Return</div>
              <div class="stat-val" style="color:{gc}">{gs}${abs(total_ret):,.0f}<br>
                <span style="font-size:13px">{gs}{abs(total_pct):.2f}%</span>
              </div>
            </div>
            <div class="stat">
              <div class="stat-label">Day P&amp;L</div>
              <div class="stat-val" style="color:{dc}">${day_pnl:+,.0f}</div>
            </div>
            <div class="stat">
              <div class="stat-label">Open Positions</div>
              <div class="stat-val">{len(open_pos)}</div>
            </div>
            <div class="stat">
              <div class="stat-label">Win Rate</div>
              <div class="stat-val">{wr:.1f}%</div>
            </div>
            <div class="stat">
              <div class="stat-label">Closed Trades</div>
              <div class="stat-val">{len(all_c)}</div>
            </div>
          </div>
          {chart_section}
          <div style="font-size:11px;color:#999;margin-bottom:12px">
            Inception: {inception} &nbsp;|&nbsp; Last run: {last_run}
          </div>
          <a href="{today_report}" class="btn" style="background:{color}">Open Dashboard →</a>
        </div>"""


def build_hub():
    today_str = date.today().isoformat()

    swing    = _load(SWING_FILE)
    intraday = _load(INTRADAY_FILE)

    # Find latest report files
    reports = BASE_DIR / "reports"
    swing_reports    = sorted(reports.glob("paper_*.html"),    reverse=True) if reports.exists() else []
    intraday_reports = sorted(reports.glob("intraday_*.html"), reverse=True) if reports.exists() else []

    swing_link    = f"reports/{swing_reports[0].name}"    if swing_reports    else "paper_index.html"
    intraday_link = f"reports/{intraday_reports[0].name}" if intraday_reports else "intraday_index.html"

    swing_card    = _bot_card("Swing Trader (RSI-2)",   "📈", "#1a237e", "swing-chart",    swing,    swing_link,    "paper_index.html")
    intraday_card = _bot_card("Intraday Trader (ORB)",  "⚡", "#e65100", "intraday-chart", intraday, intraday_link, "intraday_index.html")

    swing_js    = _nav_chart_js("swing-chart",    swing.get("nav_history",    {}) if swing    else {}, "#1a237e")
    intraday_js = _nav_chart_js("intraday-chart", intraday.get("nav_history", {}) if intraday else {}, "#e65100")

    # Screener link
    screener_reports = sorted(reports.glob("[0-9]*.html"), reverse=True) if reports.exists() else []
    screener_link    = f"reports/{screener_reports[0].name}" if screener_reports else "index.html"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Hub — {today_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; padding: 20px 24px; background: #f0f2f5; color: #222; }}
    h1   {{ color: #1a237e; margin: 0 0 4px; font-size: 24px; }}
    .subtitle {{ color: #666; font-size: 13px; margin: 0 0 20px; }}
    .bots {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    @media (max-width: 720px) {{ .bots {{ grid-template-columns: 1fr; }} }}
    .bot-card {{ background: white; border-radius: 12px; padding: 20px 22px;
                 border: 1px solid #e0e0e0; display: flex; flex-direction: column; }}
    .bot-title {{ font-size: 18px; font-weight: bold; color: #1a237e; margin-bottom: 14px; }}
    .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 4px; }}
    .stat {{ background: #f8f9ff; border-radius: 8px; padding: 10px 12px; }}
    .stat-label {{ font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: .4px; margin-bottom: 4px; }}
    .stat-val {{ font-size: 20px; font-weight: bold; line-height: 1.2; }}
    .btn {{ display: inline-block; padding: 10px 20px; border-radius: 8px; color: white;
            text-decoration: none; font-weight: bold; font-size: 14px; text-align: center;
            margin-top: auto; transition: opacity .15s; }}
    .btn:hover {{ opacity: .85; }}
    .screener-link {{ background: white; border: 1px solid #e0e0e0; border-top: 4px solid #388e3c;
                      border-radius: 12px; padding: 16px 22px; margin-top: 20px;
                      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }}
    .badge {{ background: #e3f2fd; color: #0d47a1; padding: 3px 10px;
              border-radius: 12px; font-size: 12px; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>🏦 Trading Hub</h1>
  <p class="subtitle">Two paper-trading bots, $100k each &nbsp;|&nbsp; {today_str} &nbsp;|&nbsp; Not real money</p>

  <div class="bots">
    {swing_card}
    {intraday_card}
  </div>

  <!-- Factor Screener link -->
  <div class="screener-link">
    <div>
      <div style="font-size:16px;font-weight:bold;color:#1b5e20">📊 Multi-Factor Screener</div>
      <div style="font-size:12px;color:#666;margin-top:4px">S&amp;P 500 live portfolio · Momentum + ROIC + FCF + Piotroski</div>
    </div>
    <a href="{screener_link}" class="btn" style="background:#388e3c">Open Screener →</a>
  </div>

  <p style="color:#ccc;font-size:11px;margin-top:16px">
    Rebuilt each time either bot runs &nbsp;|&nbsp; Not financial advice.
  </p>

<script>
{swing_js}
{intraday_js}
</script>
</body>
</html>"""

    out = BASE_DIR / "trading_hub.html"
    out.write_text(html)
    print(f"Hub → {out}")


if __name__ == "__main__":
    build_hub()
