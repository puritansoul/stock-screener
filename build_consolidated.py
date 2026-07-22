"""
Build consolidated.html — tab switcher embedding all 6 dashboards as iframes.
Zero data logic here: all computation, live prices, and formatting live in the source pages.
"""

from pathlib import Path
from datetime import date

BASE_DIR = Path(__file__).parent


def latest(pattern: str, fallback: str = "index.html") -> str:
    reports = sorted((BASE_DIR / "reports").glob(pattern), reverse=True)
    if reports:
        return f"reports/{reports[0].name}"
    return fallback


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
    screener_v2_url = latest("v2_*.html",        "screener_v2_index.html")
    swing_v2_url   = latest("swing_v2_*.html",   "swing_v2_index.html")
    intraday_v2_url = latest("intraday_v2_*.html", "intraday_v2_index.html")

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Dashboard — {today_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; }}
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
    #frames {{ position: absolute; top: 43px; left: 0; right: 0; bottom: 0; }}
    iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
              border: none; display: none; background: white; }}
    iframe.active {{ display: block; }}
  </style>
</head>
<body>
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
  <script>
    document.querySelectorAll('.tab').forEach(tab => {{
      tab.addEventListener('click', () => {{
        document.querySelectorAll('.tab, iframe').forEach(el => el.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.frame).classList.add('active');
      }});
    }});
    // Restore last active tab
    var saved = localStorage.getItem('activeTab');
    if (saved) {{
      var t = document.querySelector('.tab[data-frame="' + saved + '"]');
      if (t) t.click();
    }}
    document.querySelectorAll('.tab').forEach(tab => {{
      tab.addEventListener('click', () => {{
        localStorage.setItem('activeTab', tab.dataset.frame);
      }});
    }});
  </script>
</body>
</html>"""

    out = BASE_DIR / "consolidated.html"
    out.write_text(html)
    print(f"Consolidated → {out}")


if __name__ == "__main__":
    build_consolidated()
