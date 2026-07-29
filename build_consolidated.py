"""
Build consolidated.html — tab switcher embedding all 6 dashboards as iframes.
Zero data logic here: all computation, live prices, and formatting live in the source pages.
"""

from pathlib import Path
from datetime import date
import json

BASE_DIR = Path(__file__).parent
STARTING_CAPITAL = 100_000.0


def latest(pattern: str, fallback: str = "index.html") -> str:
    reports = sorted((BASE_DIR / "reports").glob(pattern), reverse=True)
    if reports:
        return f"reports/{reports[0].name}"
    return fallback


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _nav_value(nav_history: dict, starting: float) -> tuple[float, float]:
    """Return (current_value, prev_value) from a nav_history dict."""
    if not nav_history:
        return starting, starting
    dates = sorted(nav_history.keys())
    cur = nav_history[dates[-1]]
    prev = nav_history[dates[-2]] if len(dates) >= 2 else starting
    return cur, prev


def _screener_nav(nav_file: Path, starting: float) -> tuple[float, float]:
    """portfolio_nav stores NAV units (1.0 = starting), convert to $."""
    d = _load_json(nav_file)
    if not d:
        return starting, starting
    dates = sorted(d.keys())
    cur_nav  = d[dates[-1]]
    prev_nav = d[dates[-2]] if len(dates) >= 2 else 1.0
    return cur_nav * starting, prev_nav * starting


def build_cross_bot_summary() -> tuple[str, str]:
    """
    Returns (summary_html, summary_js) for the cross-bot summary bar.
    summary_html is static server-rendered values.
    summary_js reads live values from iframes once they load.
    """
    today_str = date.today().isoformat()

    # Order: Factor Screener v1 → Swing v1 → Intraday v1 → V2s in same order
    bots = [
        ("Screener v1",   None, BASE_DIR / "portfolio_nav.json",            "bar-screener-v1", "port-today"),
        ("Swing v1",      _load_json(BASE_DIR / "swing_trades.json"),      None, "bar-swing-v1",    "day-pnl"),
        ("Intraday v1",   _load_json(BASE_DIR / "intraday_trades.json"),   None, "bar-intraday-v1", "port-today"),
        ("Screener v2",   None, BASE_DIR / "portfolio_nav_v2.json",         "bar-screener-v2", "port-today"),
        ("Swing v2",      _load_json(BASE_DIR / "swing_trades_v2.json"),   None, "bar-swing-v2",    "day-pnl"),
        ("Intraday v2",   _load_json(BASE_DIR / "intraday_trades_v2.json"),None, "bar-intraday-v2", "port-today"),
    ]

    total_value = 0.0
    total_prev  = 0.0
    rows_html   = ""

    # Map each bot's iframe id (for live JS sync)
    bot_iframe_map = {
        "bar-screener-v1": "screener",
        "bar-swing-v1":    "swing",
        "bar-intraday-v1": "intraday",
        "bar-screener-v2": "screener-v2",
        "bar-swing-v2":    "swing-v2",
        "bar-intraday-v2": "intraday-v2",
    }

    for name, state, nav_file, bar_id, day_el_id in bots:
        if nav_file:
            cur, prev = _screener_nav(nav_file, STARTING_CAPITAL)
        elif state:
            cur, prev = _nav_value(state.get("nav_history", {}), STARTING_CAPITAL)
        else:
            cur, prev = STARTING_CAPITAL, STARTING_CAPITAL

        total_value += cur
        total_prev  += prev
        day_d  = cur - prev
        day_p  = day_d / prev * 100 if prev else 0
        ret_d  = cur - STARTING_CAPITAL
        ret_p  = ret_d / STARTING_CAPITAL * 100
        dc = "#2e7d32" if day_d >= 0 else "#c62828"
        rc = "#2e7d32" if ret_d >= 0 else "#c62828"
        ds = "+" if day_d >= 0 else "-"
        rs = "+" if ret_d >= 0 else "-"
        rows_html += f"""<div id="{bar_id}" data-day-el="{day_el_id}" style="display:flex;gap:18px;align-items:center;padding:4px 12px;border-right:1px solid rgba(255,255,255,.15)">
          <span style="font-size:11px;color:rgba(255,255,255,.6);min-width:72px">{name}</span>
          <span id="{bar_id}-val" style="font-weight:bold;color:white">${cur:,.0f}</span>
          <span id="{bar_id}-day" style="color:{dc};font-size:12px">{ds}${abs(day_d):,.0f} ({ds}{abs(day_p):.1f}%)</span>
          <span id="{bar_id}-ret" style="color:{rc};font-size:12px">{rs}{abs(ret_p):.1f}% all-time</span>
        </div>"""

    total_day_d = total_value - total_prev
    total_day_p = total_day_d / total_prev * 100 if total_prev else 0
    total_ret_d = total_value - STARTING_CAPITAL * 6
    total_ret_p = total_ret_d / (STARTING_CAPITAL * 6) * 100
    tdc = "#90caf9" if total_day_d >= 0 else "#ef9a9a"
    trc = "#90caf9" if total_ret_d >= 0 else "#ef9a9a"
    tds = "+" if total_day_d >= 0 else "-"
    trs = "+" if total_ret_d >= 0 else "-"

    summary_html = f"""<div id="summary-bar" style="background:#0d1b6e;border-bottom:1px solid rgba(255,255,255,.1);padding:4px 16px;display:flex;align-items:center;gap:0;overflow-x:auto;white-space:nowrap;font-size:12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="display:flex;gap:12px;align-items:center;padding:4px 16px 4px 0;border-right:1px solid rgba(255,255,255,.25);margin-right:4px">
    <span style="font-size:11px;font-weight:bold;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:.5px">All Bots</span>
    <span id="bar-total-val" style="font-weight:bold;color:white;font-size:14px">${total_value:,.0f}</span>
    <span id="bar-total-day" style="color:{tdc}">{tds}${abs(total_day_d):,.0f} today ({tds}{abs(total_day_p):.1f}%)</span>
    <span id="bar-total-ret" style="color:{trc}">{trs}{abs(total_ret_p):.1f}% all-time</span>
  </div>
  {rows_html}
  <div id="summary-stale" style="margin-left:auto;padding-left:16px;font-size:11px;color:rgba(255,255,255,.4)">as of last run · {today_str}</div>
</div>"""

    return summary_html


def build_consolidated():
    today_str = date.today().isoformat()

    # v1 screener: dated files that aren't swing_/intraday_/v2_/paper_
    screener_reports = sorted(
        (r for r in (BASE_DIR / "reports").glob("*.html")
         if not r.name.startswith("swing_")
         and not r.name.startswith("intraday_")
         and not r.name.startswith("v2_")
         and not r.name.startswith("paper_")),
        reverse=True
    )
    screener_url   = f"reports/{screener_reports[0].name}" if screener_reports else "index.html"

    swing_reports = sorted(
        (r for r in (BASE_DIR / "reports").glob("swing_*.html")
         if not r.name.startswith("swing_v2_")),
        reverse=True
    )
    swing_url = f"reports/{swing_reports[0].name}" if swing_reports else "swing_index.html"

    intraday_reports = sorted(
        (r for r in (BASE_DIR / "reports").glob("intraday_*.html")
         if not r.name.startswith("intraday_v2_")),
        reverse=True
    )
    intraday_url = f"reports/{intraday_reports[0].name}" if intraday_reports else "intraday_index.html"
    screener_v2_url  = latest("v2_*.html",           "screener_v2_index.html")
    swing_v2_url     = latest("swing_v2_*.html",     "swing_v2_index.html")
    intraday_v2_url  = latest("intraday_v2_*.html",  "intraday_v2_index.html")

    summary_html = build_cross_bot_summary()

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Dashboard — {today_str}</title>
  <style>
    *, *::before, *:: after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; }}
    #summary-bar::-webkit-scrollbar {{ height: 4px; }}
    #summary-bar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,.2); border-radius: 2px; }}
    #tabs {{ display: flex; gap: 0; background: #1a237e; padding: 0 16px; align-items: stretch; overflow-x: auto; }}
    .tab {{ padding: 12px 18px; color: rgba(255,255,255,.65); cursor: pointer; font-size: 13px;
            font-weight: 600; border-bottom: 3px solid transparent; white-space: nowrap;
            transition: color .15s, border-color .15s; user-select: none; }}
    .tab:hover  {{ color: white; }}
    .tab.active {{ color: white; border-bottom-color: white; }}
    .tab-sep {{ width: 1px; background: rgba(255,255,255,.2); margin: 8px 4px; }}
    .tab-v2 {{ color: rgba(144,202,249,.75); }}
    .tab-v2.active {{ color: #90caf9; border-bottom-color: #90caf9; }}
    .tab-v2:hover  {{ color: #90caf9; }}
    #frames {{ position: absolute; top: 87px; left: 0; right: 0; bottom: 0; }}
    iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
              border: none; display: none; background: white; }}
    iframe.active {{ display: block; }}
    #stale-banner {{ display:none; position:fixed; bottom:16px; right:16px; z-index:999;
                     background:#b71c1c; color:white; padding:8px 16px; border-radius:8px;
                     font-size:13px; font-weight:bold; box-shadow:0 2px 8px rgba(0,0,0,.3); }}
  </style>
</head>
<body>
  {summary_html}
  <div id="tabs">
    <div class="tab active"    data-frame="screener">📊 Factor Screener</div>
    <div class="tab"           data-frame="swing">📈 Swing Trader</div>
    <div class="tab"           data-frame="intraday">⚡ Intraday Trader</div>
    <div class="tab-sep"></div>
    <div class="tab tab-v2"   data-frame="screener-v2">📊 Screener v2</div>
    <div class="tab tab-v2"   data-frame="swing-v2">📈 Swing v2</div>
    <div class="tab tab-v2"   data-frame="intraday-v2">⚡ Intraday v2</div>
  </div>
  <div id="frames">
    <iframe id="screener"    class="active" src="{screener_url}"></iframe>
    <iframe id="swing"       src="{swing_url}"></iframe>
    <iframe id="intraday"    src="{intraday_url}"></iframe>
    <iframe id="screener-v2" src="{screener_v2_url}"></iframe>
    <iframe id="swing-v2"    src="{swing_v2_url}"></iframe>
    <iframe id="intraday-v2" src="{intraday_v2_url}"></iframe>
  </div>
  <div id="stale-banner">⚠ Prices may be stale — last update &gt;10 min ago</div>

<script>
// ── Tab switching ─────────────────────────────────────────────────────────────
(function() {{
  document.querySelectorAll('.tab').forEach(tab => {{
    tab.addEventListener('click', () => {{
      document.querySelectorAll('.tab, iframe').forEach(el => el.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.frame).classList.add('active');
      localStorage.setItem('activeTab', tab.dataset.frame);
    }});
  }});
  var saved = localStorage.getItem('activeTab');
  if (saved) {{
    var t = document.querySelector('.tab[data-frame="' + saved + '"]');
    if (t) t.click();
  }}
}})();

// ── Auto-refresh active iframe every 5 min during market hours ────────────────
(function() {{
  function isMarketHours() {{
    const now = new Date();
    const et  = new Date(now.toLocaleString('en-US', {{timeZone: 'America/New_York'}}));
    const day = et.getDay();
    if (day === 0 || day === 6) return false;
    const mins = et.getHours() * 60 + et.getMinutes();
    return mins >= 570 && mins < 960; // 9:30–4:00 ET
  }}

  function refreshActive() {{
    if (!isMarketHours()) return;
    const active = document.querySelector('iframe.active');
    if (active && active.src) {{
      const src = active.src;
      active.src = '';
      setTimeout(() => {{ active.src = src; }}, 50);
    }}
  }}

  setInterval(refreshActive, 5 * 60 * 1000); // every 5 min
}})();

// ── Stale price warning ───────────────────────────────────────────────────────
(function() {{
  let lastActivity = Date.now();
  const banner = document.getElementById('stale-banner');

  function isMarketHours() {{
    const et  = new Date(new Date().toLocaleString('en-US', {{timeZone: 'America/New_York'}}));
    const day = et.getDay();
    if (day === 0 || day === 6) return false;
    const mins = et.getHours() * 60 + et.getMinutes();
    return mins >= 570 && mins < 960;
  }}

  // Listen for price-status updates inside iframes
  window.addEventListener('message', e => {{
    if (e.data && e.data.type === 'priceUpdated') {{
      lastActivity = Date.now();
      if (banner) banner.style.display = 'none';
    }}
  }});

  // Also track iframe src changes as activity
  document.querySelectorAll('iframe').forEach(f => {{
    f.addEventListener('load', () => {{ lastActivity = Date.now(); }});
  }});

  setInterval(() => {{
    if (!isMarketHours()) {{ if (banner) banner.style.display = 'none'; return; }}
    const stale = Date.now() - lastActivity > 10 * 60 * 1000;
    if (banner) banner.style.display = stale ? 'block' : 'none';
  }}, 60 * 1000);
}})();

// ── Live summary bar sync ─────────────────────────────────────────────────────
(function() {{
  const STARTING = {STARTING_CAPITAL};
  // barId → {{iframeId, dayElId}}
  const BOTS = [
    {{barId:'bar-screener-v1', iframeId:'screener',    dayEl:'port-today'}},
    {{barId:'bar-swing-v1',    iframeId:'swing',       dayEl:'day-pnl'}},
    {{barId:'bar-intraday-v1', iframeId:'intraday',    dayEl:'port-today'}},
    {{barId:'bar-screener-v2', iframeId:'screener-v2', dayEl:'port-today'}},
    {{barId:'bar-swing-v2',    iframeId:'swing-v2',    dayEl:'day-pnl'}},
    {{barId:'bar-intraday-v2', iframeId:'intraday-v2', dayEl:'port-today'}},
  ];

  function parseDollars(el) {{
    if (!el) return null;
    // Try data-sort first (swing day-pnl uses this via innerHTML containing $)
    const raw = el.textContent.replace(/[^0-9.+-]/g, '');
    const n = parseFloat(raw);
    return isNaN(n) ? null : (el.textContent.trim().startsWith('-') ? -Math.abs(n) : n);
  }}

  function fmtVal(n) {{ return '$' + Math.abs(n).toLocaleString('en-US', {{maximumFractionDigits:0}}); }}
  function sign(n) {{ return n >= 0 ? '+' : ''; }}
  function col(n) {{ return n >= 0 ? '#2e7d32' : '#c62828'; }}

  function syncBar() {{
    let totalVal = 0, totalDay = 0, anyLive = false;
    BOTS.forEach(b => {{
      try {{
        const iframe = document.getElementById(b.iframeId);
        if (!iframe || !iframe.contentDocument) return;
        const doc = iframe.contentDocument;

        const valEl = doc.getElementById('port-value');
        const dayEl = doc.getElementById(b.dayEl);
        if (!valEl) return;

        // Portfolio value
        const curVal = parseFloat(valEl.textContent.replace(/[^0-9.]/g, '')) || null;
        if (curVal === null) return;

        // Day P&L — try data-realized attr first (intraday), else parse text
        let dayPnl = null;
        if (dayEl) {{
          if (dayEl.dataset && dayEl.dataset.realized !== undefined) {{
            dayPnl = parseFloat(dayEl.dataset.realized);
          }} else {{
            const txt = dayEl.textContent.trim();
            const sign_ = txt.startsWith('-') ? -1 : 1;
            const num = parseFloat(txt.replace(/[^0-9.]/g, ''));
            if (!isNaN(num)) dayPnl = sign_ * num;
          }}
        }}

        anyLive = true;
        totalVal += curVal;
        if (dayPnl !== null) totalDay += dayPnl;

        const retD  = curVal - STARTING;
        const retP  = retD / STARTING * 100;
        const prevVal = dayPnl !== null ? curVal - dayPnl : STARTING;
        const dayP  = prevVal > 0 && dayPnl !== null ? dayPnl / prevVal * 100 : 0;

        const valSpan = document.getElementById(b.barId + '-val');
        const daySpan = document.getElementById(b.barId + '-day');
        const retSpan = document.getElementById(b.barId + '-ret');
        if (valSpan) valSpan.textContent = fmtVal(curVal);
        if (daySpan && dayPnl !== null) {{
          daySpan.textContent = sign(dayPnl) + fmtVal(dayPnl) + ' (' + sign(dayP) + Math.abs(dayP).toFixed(1) + '%)';
          daySpan.style.color = col(dayPnl);
        }}
        if (retSpan) {{
          retSpan.textContent = sign(retD) + Math.abs(retP).toFixed(1) + '% all-time';
          retSpan.style.color = col(retD);
        }}
      }} catch(e) {{ /* cross-origin or not-yet-loaded, skip */ }}
    }});

    if (!anyLive) return;

    const totalRet  = totalVal - STARTING * BOTS.length;
    const totalRetP = totalRet / (STARTING * BOTS.length) * 100;
    const totalPrev = totalVal - totalDay;
    const totalDayP = totalPrev > 0 ? totalDay / totalPrev * 100 : 0;

    const tv = document.getElementById('bar-total-val');
    const td = document.getElementById('bar-total-day');
    const tr = document.getElementById('bar-total-ret');
    if (tv) tv.textContent = fmtVal(totalVal);
    if (td) {{
      td.textContent = sign(totalDay) + fmtVal(totalDay) + ' today (' + sign(totalDayP) + Math.abs(totalDayP).toFixed(1) + '%)';
      td.style.color = totalDay >= 0 ? '#90caf9' : '#ef9a9a';
    }}
    if (tr) {{
      tr.textContent = sign(totalRet) + Math.abs(totalRetP).toFixed(1) + '% all-time';
      tr.style.color = totalRet >= 0 ? '#90caf9' : '#ef9a9a';
    }}

    // Update stale label to show live
    const sl = document.getElementById('summary-stale');
    if (sl) sl.textContent = 'live';
  }}

  // Sync after iframes load and every 30s
  document.querySelectorAll('iframe').forEach(f => {{
    f.addEventListener('load', () => setTimeout(syncBar, 500));
  }});
  setInterval(syncBar, 30 * 1000);
  setTimeout(syncBar, 2000); // initial attempt after page settles
}})();
</script>
</body>
</html>"""

    out = BASE_DIR / "consolidated.html"
    out.write_text(html)
    print(f"Consolidated → {out}")


if __name__ == "__main__":
    build_consolidated()
