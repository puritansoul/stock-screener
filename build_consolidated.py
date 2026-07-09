"""
Build consolidated.html — one-page view of all three trackers:
  1. Factor Screener  (portfolio_state.json + portfolio_nav.json)
  2. Swing Trader     (paper_trades.json)
  3. Intraday Trader  (intraday_trades.json)

Called by screener.yml, prices.yml, paper_trading.yml, intraday_trading.yml.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

BASE_DIR      = Path(__file__).parent
FINNHUB_TOKEN = "d93d6v1r01qgqnua64j0d93d6v1r01qgqnua64jg"


def _load(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _sparkline_js(var: str, nav_items: list[tuple[str, float]], color: str) -> str:
    values = [v for _, v in nav_items]
    return f"""
(function() {{
  const vals = {json.dumps(values)};
  if (vals.length < 2) return;
  const canvas = document.getElementById('{var}');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 340, H = 70;
  canvas.width = W; canvas.height = H;
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const range = maxV - minV || 1, pad = 6;
  ctx.strokeStyle = '{color}'; ctx.lineWidth = 1.5;
  ctx.beginPath();
  vals.forEach((v, i) => {{
    const x = pad + (i / (vals.length - 1)) * (W - 2*pad);
    const y = H - pad - ((v - minV) / range) * (H - 2*pad);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.lineTo(pad + (W-2*pad), H-pad); ctx.lineTo(pad, H-pad); ctx.closePath();
  const g = ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0, '{color}22'); g.addColorStop(1, '{color}00');
  ctx.fillStyle = g; ctx.fill();
}})();"""


def _color(val: float) -> str:
    return "#2e7d32" if val >= 0 else "#c62828"

def _sign(val: float) -> str:
    return "+" if val >= 0 else ""


def _screener_card(state: dict | None, nav_raw: dict | None) -> tuple[str, str]:
    if not state:
        return ('<div class="tracker-card" id="screener-card" style="border-top:4px solid #388e3c">'
                '<div class="tracker-title">📊 Factor Screener</div>'
                '<div class="no-data">No data yet</div></div>', "")

    holdings  = state.get("holdings", [])
    positions = state.get("positions", {})
    scores    = state.get("scores", {})
    last_run  = state.get("last_run", "—")
    last_rebal= state.get("last_rebalance", "—")
    inception = state.get("inception_date", "—")

    # cost_basis = what was actually invested (screener doesn't use $100k flat)
    cost_basis = sum(v.get("shares", 0) * v.get("price", 0) for v in positions.values()) or 100_000.0

    # nav_raw stores multipliers (e.g. 1.025)
    nav = {d: v * cost_basis for d, v in (nav_raw or {}).items()}
    today_str = date.today().isoformat()
    port_val  = nav.get(today_str, cost_basis)
    total_ret = port_val - cost_basis
    total_pct = total_ret / cost_basis * 100
    gc = _color(total_ret); gs = _sign(total_ret)

    # All holdings sorted by score
    held_scores = sorted(
        [(tk, scores[tk]["composite"]) for tk in holdings if tk in scores],
        key=lambda x: -x[1]
    )

    pos_rows = ""
    for tk, comp in held_scores:
        pos   = positions.get(tk, {})
        px    = pos.get("price", 0)
        shrs  = pos.get("shares", 0)
        cost  = px * shrs
        pos_rows += (
            f'<tr data-ticker="{tk}" data-qty="{shrs}" data-buypx="{px:.2f}" data-side="long">'
            f'<td style="font-weight:bold">{tk}</td>'
            f'<td style="text-align:right" class="live-price">${px:,.2f}</td>'
            f'<td style="text-align:right">{shrs}</td>'
            f'<td style="text-align:right" class="live-invested">${cost:,.0f}</td>'
            f'<td style="text-align:right;color:#1a237e;font-weight:bold">{comp:.3f}</td>'
            f'</tr>'
        )
    if not pos_rows:
        pos_rows = '<tr><td colspan="5" style="text-align:center;color:#999;padding:8px">No holdings</td></tr>'

    nav_items = sorted(nav.items())
    spark_js  = _sparkline_js("screener-spark", nav_items, "#388e3c")

    card = f"""
    <div class="tracker-card" id="screener-card" style="border-top:4px solid #388e3c"
         data-cash="0" data-basis="{cost_basis:.2f}">
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
      <div style="max-height:220px;overflow-y:auto">
      <table class="mini-table">
        <thead><tr><th>Ticker</th><th>Price</th><th>Shares</th><th>Value</th><th>Score</th></tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
      </div>
      <div class="meta-row">
        <span>Last run: {last_run}</span>
        <span>Rebalance: {last_rebal}</span>
        <span>Since: {inception}</span>
      </div>
      <a href="index.html" class="dash-link" style="background:#388e3c">Full Screener →</a>
    </div>"""

    return card, spark_js


def _swing_card(state: dict | None) -> tuple[str, str]:
    if not state:
        return ('<div class="tracker-card" id="swing-card" style="border-top:4px solid #1a237e">'
                '<div class="tracker-title">📈 Swing Trader</div>'
                '<div class="no-data">No data yet</div></div>', "")

    open_pos  = state.get("open_positions", [])
    closed    = state.get("closed_positions", [])
    nav       = state.get("nav_history", {})
    capital   = state.get("capital", 100_000.0)
    inception = state.get("inception_date", "—")
    last_log  = state.get("log", [{}])[-1].get("date", "—") if state.get("log") else "—"

    today_str = date.today().isoformat()
    port_val  = nav.get(today_str, 100_000.0)
    total_ret = port_val - 100_000.0
    total_pct = total_ret / 100_000.0 * 100
    gc = _color(total_ret); gs = _sign(total_ret)

    wins     = [p for p in closed if p.get("pnl", 0) > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    pos_rows = ""
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        pos_rows += (
            f'<tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">'
            f'<td style="font-weight:bold">{tk}</td>'
            f'<td style="font-size:11px;color:{"#1b5e20" if side=="long" else "#880e4f"}">{side.upper()}</td>'
            f'<td style="text-align:right">${entry:,.2f}</td>'
            f'<td style="text-align:right" class="live-price">${entry:,.2f}</td>'
            f'<td style="text-align:right" class="live-unreal">—</td>'
            f'</tr>'
        )
    if not pos_rows:
        pos_rows = '<tr><td colspan="5" style="text-align:center;color:#999;padding:8px">No open positions</td></tr>'

    nav_items = sorted(nav.items())
    spark_js  = _sparkline_js("swing-spark", nav_items, "#1a237e")

    card = f"""
    <div class="tracker-card" id="swing-card" style="border-top:4px solid #1a237e"
         data-cash="{capital:.2f}" data-basis="100000">
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
          <div class="kpi-val">{win_rate:.0f}%<br><span style="font-size:11px;color:#888">{len(closed)} closed</span></div>
        </div>
      </div>
      <canvas id="swing-spark" style="width:100%;margin:8px 0 4px" height="70"></canvas>
      <div style="max-height:220px;overflow-y:auto">
      <table class="mini-table">
        <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th><th>P&amp;L</th></tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
      </div>
      <div class="meta-row">
        <span>Cash: ${capital:,.0f}</span>
        <span>{len(open_pos)} open / {len(closed)} closed</span>
        <span>Since: {inception}</span>
      </div>
      <a href="paper_index.html" class="dash-link" style="background:#1a237e">Full Dashboard →</a>
    </div>"""

    return card, spark_js


def _intraday_card(state: dict | None) -> tuple[str, str]:
    if not state:
        return ('<div class="tracker-card" id="intraday-card" style="border-top:4px solid #e65100">'
                '<div class="tracker-title">⚡ Intraday Trader</div>'
                '<div class="no-data">No data yet</div></div>', "")

    open_pos   = state.get("open_positions", [])
    closed_td  = state.get("closed_today", [])
    all_closed = state.get("all_closed", [])
    nav        = state.get("nav_history", {})
    capital    = state.get("capital", 100_000.0)
    inception  = state.get("inception_date", "—")

    today_str = date.today().isoformat()
    port_val  = nav.get(today_str, 100_000.0)
    total_ret = port_val - 100_000.0
    total_pct = total_ret / 100_000.0 * 100

    day_pnl = sum(p.get("pnl", 0) for p in closed_td)
    dc = _color(day_pnl)

    all_c    = all_closed + closed_td
    wins     = [p for p in all_c if p.get("pnl", 0) > 0]
    win_rate = len(wins) / len(all_c) * 100 if all_c else 0

    pos_rows = ""
    for pos in open_pos:
        tk    = pos["ticker"]
        side  = pos["side"]
        entry = pos["entry_price"]
        shares= pos["shares"]
        pos_rows += (
            f'<tr data-ticker="{tk}" data-qty="{shares}" data-buypx="{entry:.2f}" data-side="{side}">'
            f'<td style="font-weight:bold">{tk}</td>'
            f'<td style="font-size:11px;color:{"#1b5e20" if side=="long" else "#880e4f"}">{side.upper()}</td>'
            f'<td style="text-align:right">${entry:,.2f}</td>'
            f'<td style="text-align:right" class="live-price">${entry:,.2f}</td>'
            f'<td style="text-align:right" class="live-unreal">—</td>'
            f'</tr>'
        )

    closed_rows = ""
    for pos in reversed(closed_td[-5:]):
        tk  = pos["ticker"]
        pnl = pos.get("pnl", 0)
        pc  = _color(pnl)
        closed_rows += (
            f'<tr><td style="font-weight:bold">{tk}</td>'
            f'<td style="text-align:right;color:{pc};font-weight:bold">${pnl:+,.0f}</td>'
            f'<td style="color:#888;font-size:11px">{pos.get("exit_reason","—")}</td></tr>'
        )

    trades_html = ""
    if pos_rows:
        trades_html += f"""<div style="max-height:160px;overflow-y:auto">
        <table class="mini-table">
          <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th><th>P&amp;L</th></tr></thead>
          <tbody>{pos_rows}</tbody></table></div>"""
    if closed_rows:
        trades_html += f"""<div style="font-size:11px;color:#888;margin:6px 0 2px">Closed today:</div>
        <table class="mini-table">
          <thead><tr><th>Ticker</th><th>P&amp;L</th><th>Reason</th></tr></thead>
          <tbody>{closed_rows}</tbody></table>"""
    if not trades_html:
        trades_html = '<div style="color:#bbb;font-size:12px;padding:8px 0">No trades today</div>'

    nav_items = sorted(nav.items())
    spark_js  = _sparkline_js("intraday-spark", nav_items, "#e65100")

    card = f"""
    <div class="tracker-card" id="intraday-card" style="border-top:4px solid #e65100"
         data-cash="{capital:.2f}" data-basis="100000">
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
      {trades_html}
      <div class="meta-row">
        <span>Cash: ${capital:,.0f}</span>
        <span>{len(open_pos)} open today</span>
        <span>Since: {inception}</span>
      </div>
      <a href="intraday_index.html" class="dash-link" style="background:#e65100">Full Dashboard →</a>
    </div>"""

    return card, spark_js


LIVE_JS = f"""
(function() {{
  const TOKEN = '{FINNHUB_TOKEN}';
  const delay = ms => new Promise(r => setTimeout(r, ms));

  function isMarketHours() {{
    const et = new Date(new Date().toLocaleString('en-US', {{timeZone:'America/New_York'}}));
    const day = et.getDay();
    if (day === 0 || day === 6) return false;
    const mins = et.getHours() * 60 + et.getMinutes();
    return mins >= 570 && mins < 960;
  }}

  async function fetchAll() {{
    const rows = document.querySelectorAll('tr[data-ticker]');
    if (!rows.length) return;
    // deduplicate tickers across all 3 cards
    const tkSet = new Set();
    rows.forEach(r => tkSet.add(r.dataset.ticker));
    const quotes = {{}};
    for (const tk of tkSet) {{
      try {{
        const q = await fetch(`https://finnhub.io/api/v1/quote?symbol=${{tk}}&token=${{TOKEN}}`).then(r => r.json());
        if (q && q.c) quotes[tk] = q.c;
      }} catch(e) {{}}
      await delay(1100);
    }}

    // update every row's price and P&L cells
    rows.forEach(row => {{
      const tk  = row.dataset.ticker;
      const qty = parseFloat(row.dataset.qty);
      const buy = parseFloat(row.dataset.buypx);
      const side= row.dataset.side;
      const px  = quotes[tk];
      if (!px) return;
      const priceEl  = row.querySelector('.live-price');
      const unrealEl = row.querySelector('.live-unreal');
      const investEl = row.querySelector('.live-invested');
      if (priceEl)  priceEl.textContent  = '$' + px.toLocaleString('en-US', {{minimumFractionDigits:2,maximumFractionDigits:2}});
      if (investEl) investEl.textContent = '$' + (px*qty).toLocaleString('en-US',{{maximumFractionDigits:0}});
      if (unrealEl) {{
        const unreal = side==='long' ? (px-buy)*qty : (buy-px)*qty;
        const pct    = (buy*qty) > 0 ? unreal/(buy*qty)*100 : 0;
        unrealEl.textContent = (unreal>=0?'+':'') + '$' + Math.abs(Math.round(unreal)).toLocaleString('en-US') +
          ' (' + (pct>=0?'+':'') + pct.toFixed(2) + '%)';
        unrealEl.style.color = unreal >= 0 ? '#2e7d32' : '#c62828';
      }}
    }});

    // recompute portfolio value for each card using data-cash + sum of live position values
    ['screener-card','swing-card','intraday-card'].forEach(cardId => {{
      const card  = document.getElementById(cardId);
      if (!card) return;
      const cash  = parseFloat(card.dataset.cash) || 0;
      const basis = parseFloat(card.dataset.basis) || 100000;
      let openVal = 0;
      card.querySelectorAll('tr[data-ticker]').forEach(row => {{
        const tk  = row.dataset.ticker;
        const qty = parseFloat(row.dataset.qty);
        const buy = parseFloat(row.dataset.buypx);
        const side= row.dataset.side;
        const px  = quotes[tk];
        if (px) openVal += side==='long' ? px*qty : buy*qty + (buy-px)*qty;
        else    openVal += buy*qty;
      }});
      const portVal = cash + openVal;
      const ret     = portVal - basis;
      const retPct  = ret / basis * 100;
      const sign    = ret >= 0 ? '+' : '';
      const color   = ret >= 0 ? '#2e7d32' : '#c62828';
      const portEl  = card.querySelector('[id$="-port-value"]');
      const totalEl = card.querySelector('[id$="-port-total"]');
      if (portEl)  portEl.textContent = '$' + portVal.toLocaleString('en-US', {{maximumFractionDigits:0}});
      if (totalEl) {{
        totalEl.innerHTML = sign + '$' + Math.abs(ret).toLocaleString('en-US',{{maximumFractionDigits:0}}) +
          '<br><span style="font-size:12px">' + sign + Math.abs(retPct).toFixed(2) + '%</span>';
        totalEl.style.color = color;
      }}
    }});

    const t = new Date().toLocaleTimeString('en-US',{{timeZone:'America/New_York'}});
    const el = document.getElementById('last-updated');
    if (el) el.textContent = 'Prices updated ' + t + ' ET';
  }}

  if (isMarketHours()) {{
    fetchAll();
    setInterval(fetchAll, 60000);
  }}
}})();"""


def build_consolidated():
    today_str = date.today().isoformat()

    screener_card, screener_js = _screener_card(
        _load(BASE_DIR / "portfolio_state.json"),
        _load(BASE_DIR / "portfolio_nav.json"),
    )
    swing_card,    swing_js    = _swing_card(_load(BASE_DIR / "paper_trades.json"))
    intraday_card, intraday_js = _intraday_card(_load(BASE_DIR / "intraday_trades.json"))

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
    @media (max-width: 900px)  {{ .grid {{ grid-template-columns: 1fr; }} }}
    @media (min-width: 901px) and (max-width: 1200px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
    .tracker-card {{ background: white; border-radius: 10px; border: 1px solid #e0e0e0;
                     padding: 16px 18px; display: flex; flex-direction: column; gap: 8px; }}
    .tracker-title {{ font-size: 15px; font-weight: bold; color: #1a237e; }}
    .kpi-row  {{ display: flex; gap: 8px; }}
    .kpi      {{ background: #f8f9ff; border-radius: 6px; padding: 8px 10px; flex: 1; min-width: 0; }}
    .kpi-label {{ font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: .4px; }}
    .kpi-val  {{ font-size: 18px; font-weight: bold; line-height: 1.2; margin-top: 2px; }}
    .mini-table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    .mini-table th {{ background: #f0f4ff; color: #555; padding: 5px 7px; text-align: left;
                      font-size: 10px; text-transform: uppercase; letter-spacing: .3px; position: sticky; top: 0; }}
    .mini-table td {{ padding: 4px 7px; border-bottom: 1px solid #f5f5f5; white-space: nowrap; }}
    .mini-table tr:hover td {{ background: #fafafa; }}
    .meta-row {{ font-size: 11px; color: #aaa; display: flex; gap: 10px; flex-wrap: wrap; }}
    .dash-link {{ display: block; padding: 8px 14px; border-radius: 6px; color: white;
                  text-decoration: none; font-weight: bold; font-size: 13px; text-align: center;
                  margin-top: auto; transition: opacity .15s; }}
    .dash-link:hover {{ opacity: .85; }}
    .no-data {{ color: #bbb; font-size: 13px; padding: 16px 0; }}
  </style>
</head>
<body>
  <h1>🏦 Trading Dashboard</h1>
  <p class="subtitle" id="last-updated">{today_str} &nbsp;·&nbsp; Not real money</p>

  <div class="grid">
    {screener_card}
    {swing_card}
    {intraday_card}
  </div>

<script>
{screener_js}
{swing_js}
{intraday_js}
{LIVE_JS}
</script>
</body>
</html>"""

    out = BASE_DIR / "consolidated.html"
    out.write_text(html)
    print(f"Consolidated → {out}")


if __name__ == "__main__":
    build_consolidated()
