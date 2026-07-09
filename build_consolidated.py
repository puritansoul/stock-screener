"""
Build consolidated.html — one-page view of all three trackers:
  1. Factor Screener  (portfolio_state.json + portfolio_nav.json)
  2. Swing Trader     (paper_trades.json)
  3. Intraday Trader  (intraday_trades.json)

Reads state files committed by each workflow.
Called by screener.yml, paper_trading.yml, and intraday_trading.yml.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

BASE_DIR         = Path(__file__).parent
STARTING_CAPITAL = 100_000.0
FINNHUB_TOKEN    = "d93d6v1r01qgqnua64j0d93d6v1r01qgqnua64jg"


def _load(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _sparkline_js(var: str, nav: dict, color: str, height: int = 70) -> str:
    dates  = sorted(nav.keys())
    values = [nav[d] for d in dates]
    # normalise nav to dollar value — screener stores multipliers, bots store raw $
    if values and max(values) < 100:
        values = [v * STARTING_CAPITAL for v in values]
    return f"""
(function() {{
  const vals = {json.dumps(values)};
  const N = vals.length;
  if (N < 2) return;
  const canvas = document.getElementById('{var}');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 340, H = {height};
  canvas.width = W; canvas.height = H;
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const range = maxV - minV || 1, pad = 6;
  ctx.strokeStyle = '{color}'; ctx.lineWidth = 1.5;
  ctx.beginPath();
  vals.forEach((v, i) => {{
    const x = pad + (i / (N-1)) * (W - 2*pad);
    const y = H - pad - ((v - minV) / range) * (H - 2*pad);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.lineTo(pad + (W-2*pad), H-pad); ctx.lineTo(pad, H-pad); ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, '{color}22'); g.addColorStop(1, '{color}00');
  ctx.fillStyle = g; ctx.fill();
}})();"""


def _pct_badge(val: float) -> str:
    sign  = "+" if val >= 0 else ""
    color = "#2e7d32" if val >= 0 else "#c62828"
    bg    = "#e8f5e9" if val >= 0 else "#fce4ec"
    return f'<span style="background:{bg};color:{color};padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold">{sign}{val:.2f}%</span>'


def _screener_card(state: dict | None, nav_raw: dict | None) -> tuple[str, str]:
    if not state:
        return '<div class="tracker-card" id="screener-card"><div class="tracker-title">📊 Factor Screener</div><div class="no-data">No data yet</div></div>', ""

    holdings    = state.get("holdings", [])
    positions   = state.get("positions", {})
    scores      = state.get("scores", {})
    alert_tickers = state.get("alert_tickers", [])
    last_run    = state.get("last_run", "—")
    last_rebal  = state.get("last_rebalance", "—")
    inception   = state.get("inception_date", "—")

    nav = {d: v * STARTING_CAPITAL for d, v in (nav_raw or {}).items()}
    today_str = date.today().isoformat()
    port_val = nav.get(today_str, STARTING_CAPITAL)
    total_ret = port_val - STARTING_CAPITAL
    total_pct = total_ret / STARTING_CAPITAL * 100
    gc = "#2e7d32" if total_ret >= 0 else "#c62828"
    gs = "+" if total_ret >= 0 else ""

    # Top 5 holdings by composite score
    held_scores = [(tk, scores[tk]["composite"]) for tk in holdings if tk in scores]
    held_scores.sort(key=lambda x: -x[1])

    tickers_for_live = [tk for tk in holdings if tk in positions]

    pos_rows = ""
    for tk, comp in held_scores[:8]:
        pos  = positions.get(tk, {})
        px   = pos.get("price", 0)
        shrs = pos.get("shares", 0)
        cost = px * shrs
        pos_rows += f"""<tr data-ticker="{tk}" data-qty="{shrs}" data-buypx="{px:.2f}" data-side="long">
          <td style="font-weight:bold">{tk}</td>
          <td style="text-align:right" class="live-price">${px:,.2f}</td>
          <td style="text-align:right">{shrs}</td>
          <td style="text-align:right" class="live-invested">${cost:,.0f}</td>
          <td style="text-align:right;color:#1a237e;font-weight:bold">{comp:.3f}</td>
        </tr>"""
    if len(held_scores) > 8:
        pos_rows += f'<tr><td colspan="5" style="text-align:center;color:#999;font-size:11px">+ {len(held_scores)-8} more holdings</td></tr>'

    alert_html = ""
    if alert_tickers:
        alert_html = f'<div style="margin-top:8px;font-size:12px;color:#e65100">⭐ Alerts: {", ".join(alert_tickers[:5])}</div>'

    card = f"""
    <div class="tracker-card" id="screener-card" style="border-top:4px solid #388e3c">
      <div class="tracker-title">📊 Factor Screener</div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-label">Portfolio Value</div>
          <div class="kpi-val" id="screener-port-value" style="color:#1a237e">${port_val:,.0f}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Total Return</div>
          <div class="kpi-val" id="screener-port-total" style="color:{gc}">{gs}${abs(total_ret):,.0f}<br>
            <span style="font-size:12px">{gs}{abs(total_pct):.2f}%</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Holdings</div>
          <div class="kpi-val">{len(holdings)}</div>
        </div>
      </div>
      <canvas id="screener-spark" style="width:100%;margin:8px 0 4px" height="70"></canvas>
      <table class="mini-table">
        <thead><tr><th>Ticker</th><th>Price</th><th>Shares</th><th>Value</th><th>Score</th></tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
      {alert_html}
      <div class="meta-row">
        <span>Last run: {last_run}</span>
        <span>Last rebalance: {last_rebal}</span>
        <span>Since: {inception}</span>
      </div>
      <a href="index.html" class="dash-link" style="background:#388e3c">Full Screener →</a>
    </div>"""

    js = _sparkline_js("screener-spark", nav, "#388e3c")
    return card, js, tickers_for_live


def _swing_card(state: dict | None) -> tuple[str, str]:
    if not state:
        return '<div class="tracker-card" id="swing-card"><div class="tracker-title">📈 Swing Trader</div><div class="no-data">No data yet</div></div>', "", []

    open_pos  = state.get("open_positions", [])
    closed    = state.get("closed_positions", [])
    nav       = state.get("nav_history", {})
    capital   = state.get("capital", STARTING_CAPITAL)
    inception = state.get("inception_date", "—")
    last_log  = state.get("log", [{}])[-1].get("date", "—") if state.get("log") else "—"

    today_str  = date.today().isoformat()
    port_val   = nav.get(today_str, STARTING_CAPITAL)
    total_ret  = port_val - STARTING_CAPITAL
    total_pct  = total_ret / STARTING_CAPITAL * 100
    gc = "#2e7d32" if total_ret >= 0 else "#c62828"
    gs = "+" if total_ret >= 0 else ""

    wins     = [p for p in closed if p.get("pnl", 0) > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    pos_rows = ""
    tickers_for_live = []
    for pos in open_pos[:8]:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        cost  = pos["cost"]
        tickers_for_live.append(tk)
        cur_px = entry
        if side == "long":
            unreal = (cur_px - entry) * shares
        else:
            unreal = (entry - cur_px) * shares
        uc = "#2e7d32" if unreal >= 0 else "#c62828"
        pos_rows += f"""<tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">
          <td style="font-weight:bold">{tk}</td>
          <td style="font-size:11px;color:{'#1b5e20' if side=='long' else '#880e4f'}">{side.upper()}</td>
          <td style="text-align:right">${entry:,.2f}</td>
          <td style="text-align:right" class="live-price">${cur_px:,.2f}</td>
          <td style="text-align:right" class="live-unreal" style="color:{uc}">${unreal:+,.0f}</td>
        </tr>"""
    if len(open_pos) > 8:
        pos_rows += f'<tr><td colspan="5" style="text-align:center;color:#999;font-size:11px">+ {len(open_pos)-8} more</td></tr>'
    if not pos_rows:
        pos_rows = '<tr><td colspan="5" style="text-align:center;color:#999;padding:10px">No open positions</td></tr>'

    card = f"""
    <div class="tracker-card" id="swing-card" style="border-top:4px solid #1a237e">
      <div class="tracker-title">📈 Swing Trader <span style="font-size:12px;color:#888;font-weight:normal">RSI(2)</span></div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-label">Portfolio Value</div>
          <div class="kpi-val" id="swing-port-value" style="color:#1a237e">${port_val:,.0f}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Total Return</div>
          <div class="kpi-val" id="swing-port-total" style="color:{gc}">{gs}${abs(total_ret):,.0f}<br>
            <span style="font-size:12px">{gs}{abs(total_pct):.2f}%</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Win Rate</div>
          <div class="kpi-val">{win_rate:.0f}%</div>
        </div>
      </div>
      <canvas id="swing-spark" style="width:100%;margin:8px 0 4px" height="70"></canvas>
      <table class="mini-table">
        <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th><th>P&amp;L</th></tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
      <div class="meta-row">
        <span>Cash: ${capital:,.0f}</span>
        <span>{len(open_pos)} open / {len(closed)} closed</span>
        <span>Since: {inception}</span>
      </div>
      <a href="paper_index.html" class="dash-link" style="background:#1a237e">Full Dashboard →</a>
    </div>"""

    js = _sparkline_js("swing-spark", nav, "#1a237e")
    return card, js, tickers_for_live


def _intraday_card(state: dict | None) -> tuple[str, str, list]:
    if not state:
        return '<div class="tracker-card" id="intraday-card"><div class="tracker-title">⚡ Intraday Trader</div><div class="no-data">No data yet</div></div>', "", []

    open_pos   = state.get("open_positions", [])
    closed_td  = state.get("closed_today", [])
    all_closed = state.get("all_closed", [])
    nav        = state.get("nav_history", {})
    capital    = state.get("capital", STARTING_CAPITAL)
    inception  = state.get("inception_date", "—")

    today_str  = date.today().isoformat()
    port_val   = nav.get(today_str, STARTING_CAPITAL)
    total_ret  = port_val - STARTING_CAPITAL
    total_pct  = total_ret / STARTING_CAPITAL * 100
    gc = "#2e7d32" if total_ret >= 0 else "#c62828"
    gs = "+" if total_ret >= 0 else ""

    day_pnl  = sum(p.get("pnl", 0) for p in closed_td)
    dc = "#2e7d32" if day_pnl >= 0 else "#c62828"

    all_c    = all_closed + closed_td
    wins     = [p for p in all_c if p.get("pnl", 0) > 0]
    win_rate = len(wins) / len(all_c) * 100 if all_c else 0

    pos_rows = ""
    tickers_for_live = []
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        tickers_for_live.append(tk)
        unreal = 0
        uc = "#555"
        pos_rows += f"""<tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">
          <td style="font-weight:bold">{tk}</td>
          <td style="font-size:11px;color:{'#1b5e20' if side=='long' else '#880e4f'}">{side.upper()}</td>
          <td style="text-align:right">${entry:,.2f}</td>
          <td style="text-align:right" class="live-price">${entry:,.2f}</td>
          <td style="text-align:right" class="live-unreal" style="color:{uc}">$0</td>
        </tr>"""

    closed_rows = ""
    for pos in reversed(closed_td[-5:]):
        tk   = pos["ticker"]
        pnl  = pos.get("pnl", 0)
        pc   = "#2e7d32" if pnl >= 0 else "#c62828"
        reason = pos.get("exit_reason", "—")
        closed_rows += f'<tr><td style="font-weight:bold">{tk}</td><td style="text-align:right;color:{pc};font-weight:bold">${pnl:+,.0f}</td><td style="color:#888;font-size:11px">{reason}</td></tr>'

    if not pos_rows and not closed_rows:
        pos_rows = '<tr><td colspan="5" style="text-align:center;color:#999;padding:10px">No trades today</td></tr>'

    trades_section = ""
    if pos_rows:
        trades_section += f"""<table class="mini-table">
          <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th><th>P&amp;L</th></tr></thead>
          <tbody>{pos_rows}</tbody></table>"""
    if closed_rows:
        trades_section += f"""<div style="font-size:11px;color:#888;margin:8px 0 4px">Today's closed:</div>
        <table class="mini-table"><thead><tr><th>Ticker</th><th>P&amp;L</th><th>Reason</th></tr></thead>
        <tbody>{closed_rows}</tbody></table>"""

    card = f"""
    <div class="tracker-card" id="intraday-card" style="border-top:4px solid #e65100">
      <div class="tracker-title">⚡ Intraday Trader <span style="font-size:12px;color:#888;font-weight:normal">ORB</span></div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-label">Portfolio Value</div>
          <div class="kpi-val" id="intraday-port-value" style="color:#1a237e">${port_val:,.0f}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Day P&amp;L</div>
          <div class="kpi-val" id="intraday-port-today" style="color:{dc}">${day_pnl:+,.0f}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Win Rate</div>
          <div class="kpi-val">{win_rate:.0f}%<br><span style="font-size:11px;color:#888">{len(all_c)} trades</span></div>
        </div>
      </div>
      <canvas id="intraday-spark" style="width:100%;margin:8px 0 4px" height="70"></canvas>
      {trades_section}
      <div class="meta-row">
        <span>Cash: ${capital:,.0f}</span>
        <span>{len(open_pos)} open today</span>
        <span>Since: {inception}</span>
      </div>
      <a href="intraday_index.html" class="dash-link" style="background:#e65100">Full Dashboard →</a>
    </div>"""

    js = _sparkline_js("intraday-spark", nav, "#e65100")
    return card, js, tickers_for_live


def build_consolidated():
    today_str = date.today().isoformat()

    screener_state = _load(BASE_DIR / "portfolio_state.json")
    screener_nav   = _load(BASE_DIR / "portfolio_nav.json")
    swing_state    = _load(BASE_DIR / "paper_trades.json")
    intraday_state = _load(BASE_DIR / "intraday_trades.json")

    screener_card, screener_js, screener_tickers = _screener_card(screener_state, screener_nav)
    swing_card,    swing_js,    swing_tickers    = _swing_card(swing_state)
    intraday_card, intraday_js, intraday_tickers = _intraday_card(intraday_state)

    all_tickers = list(dict.fromkeys(screener_tickers + swing_tickers + intraday_tickers))

    # Build live-price JS: single fetch loop covering all 3 trackers' positions
    live_js = f"""
(function() {{
  const TOKEN = '{FINNHUB_TOKEN}';
  const STARTING = {STARTING_CAPITAL};
  const delay = ms => new Promise(r => setTimeout(r, ms));

  function isMarketHours() {{
    const now = new Date();
    const et = new Date(now.toLocaleString('en-US', {{timeZone: 'America/New_York'}}));
    const day = et.getDay();
    if (day === 0 || day === 6) return false;
    const h = et.getHours(), m = et.getMinutes();
    return h * 60 + m >= 570 && h * 60 + m < 960;
  }}

  async function fetchAll() {{
    const rows = document.querySelectorAll('tr[data-ticker]');
    if (!rows.length) return;
    // deduplicate tickers
    const tkMap = {{}};
    rows.forEach(r => {{ tkMap[r.dataset.ticker] = true; }});
    const tickers = Object.keys(tkMap);
    const quotes = {{}};
    for (const tk of tickers) {{
      try {{
        const q = await fetch(`https://finnhub.io/api/v1/quote?symbol=${{tk}}&token=${{TOKEN}}`).then(r => r.json());
        if (q && q.c) quotes[tk] = q.c;
      }} catch(e) {{}}
      await delay(1100);
    }}
    // update every row
    rows.forEach(row => {{
      const tk   = row.dataset.ticker;
      const qty  = parseFloat(row.dataset.qty);
      const buy  = parseFloat(row.dataset.buypx);
      const side = row.dataset.side;
      const px   = quotes[tk];
      if (!px) return;
      const priceCell  = row.querySelector('.live-price');
      const unrealCell = row.querySelector('.live-unreal');
      const investCell = row.querySelector('.live-invested');
      if (priceCell)  priceCell.textContent  = '$' + px.toLocaleString('en-US', {{minimumFractionDigits:2,maximumFractionDigits:2}});
      if (investCell) investCell.textContent = '$' + (px * qty).toLocaleString('en-US', {{maximumFractionDigits:0}});
      if (unrealCell) {{
        const unreal = side === 'long' ? (px - buy) * qty : (buy - px) * qty;
        const pct = buy > 0 ? unreal / (buy * qty) * 100 : 0;
        unrealCell.textContent = '$' + (unreal >= 0 ? '+' : '') + Math.round(unreal).toLocaleString('en-US') + ' (' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)';
        unrealCell.style.color = unreal >= 0 ? '#2e7d32' : '#c62828';
      }}
    }});

    // update screener portfolio value
    updateCardPortfolio('screener', 'screener-port-value', 'screener-port-total');
    updateCardPortfolio('swing',    'swing-port-value',    'swing-port-total');
    updateCardPortfolio('intraday', 'intraday-port-value', null);

    const t = new Date().toLocaleTimeString('en-US', {{timeZone:'America/New_York'}});
    const el = document.getElementById('last-updated');
    if (el) el.textContent = 'Prices updated ' + t + ' ET';
  }}

  function updateCardPortfolio(cardPrefix, portId, totalId) {{
    const card = document.getElementById(cardPrefix + '-card');
    if (!card) return;
    const rows = card.querySelectorAll('tr[data-ticker]');
    let openVal = 0, hasAny = false;
    rows.forEach(row => {{
      const priceEl = row.querySelector('.live-price');
      if (!priceEl) return;
      const px  = parseFloat(priceEl.textContent.replace(/[$,]/g, ''));
      const qty = parseFloat(row.dataset.qty);
      if (!isNaN(px) && !isNaN(qty)) {{ openVal += px * qty; hasAny = true; }}
    }});
    if (!hasAny) return;
    const cashCard = Array.from(card.querySelectorAll('.meta-row span')).find(s => s.textContent.includes('Cash:'));
    const cashVal = cashCard ? parseFloat(cashCard.textContent.replace(/[^0-9.]/g, '')) || 0 : 0;
    const portVal = cashVal + openVal;
    const ret = portVal - STARTING;
    const retPct = ret / STARTING * 100;
    const sign = ret >= 0 ? '+' : '';
    const color = ret >= 0 ? '#2e7d32' : '#c62828';
    const portEl = document.getElementById(portId);
    if (portEl) portEl.textContent = '$' + portVal.toLocaleString('en-US', {{maximumFractionDigits:0}});
    if (totalId) {{
      const totalEl = document.getElementById(totalId);
      if (totalEl) {{
        totalEl.innerHTML = sign + '$' + Math.abs(ret).toLocaleString('en-US', {{maximumFractionDigits:0}}) + '<br><span style="font-size:12px">' + sign + Math.abs(retPct).toFixed(2) + '%</span>';
        totalEl.style.color = color;
      }}
    }}
  }}

  if (isMarketHours()) {{
    fetchAll();
    setInterval(fetchAll, 60000);
  }}
}})();"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Dashboard — {today_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; padding: 16px 20px; background: #f0f2f5; color: #222; }}
    h1   {{ color: #1a237e; margin: 0 0 2px; font-size: 22px; }}
    .subtitle {{ color: #888; font-size: 12px; margin: 0 0 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    @media (min-width: 901px) and (max-width: 1200px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
    .tracker-card {{ background: white; border-radius: 10px; border: 1px solid #e0e0e0;
                     padding: 16px 18px; display: flex; flex-direction: column; gap: 6px; }}
    .tracker-title {{ font-size: 15px; font-weight: bold; color: #1a237e; margin-bottom: 6px; }}
    .kpi-row {{ display: flex; gap: 8px; }}
    .kpi {{ background: #f8f9ff; border-radius: 6px; padding: 8px 10px; flex: 1; }}
    .kpi-label {{ font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: .4px; }}
    .kpi-val {{ font-size: 18px; font-weight: bold; line-height: 1.2; margin-top: 2px; }}
    .mini-table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 4px; }}
    .mini-table th {{ background: #f0f4ff; color: #555; padding: 5px 7px; text-align: left;
                      font-size: 10px; text-transform: uppercase; letter-spacing: .3px; }}
    .mini-table td {{ padding: 4px 7px; border-bottom: 1px solid #f5f5f5; white-space: nowrap; }}
    .mini-table tr:hover td {{ background: #fafafa; }}
    .meta-row {{ font-size: 11px; color: #aaa; display: flex; gap: 12px; flex-wrap: wrap; margin-top: 4px; }}
    .dash-link {{ display: block; padding: 8px 14px; border-radius: 6px; color: white;
                  text-decoration: none; font-weight: bold; font-size: 13px; text-align: center;
                  margin-top: auto; transition: opacity .15s; }}
    .dash-link:hover {{ opacity: .85; }}
    .no-data {{ color: #bbb; font-size: 13px; padding: 16px 0; }}
  </style>
</head>
<body>
  <h1>🏦 Trading Dashboard</h1>
  <p class="subtitle" id="last-updated">{today_str} &nbsp;·&nbsp; $100k starting capital each &nbsp;·&nbsp; Not real money</p>

  <div class="grid">
    {screener_card}
    {swing_card}
    {intraday_card}
  </div>

<script>
{screener_js}
{swing_js}
{intraday_js}
{live_js}
</script>
</body>
</html>"""

    out = BASE_DIR / "consolidated.html"
    out.write_text(html)
    print(f"Consolidated → {out}")


if __name__ == "__main__":
    build_consolidated()
