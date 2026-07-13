"""
Build consolidated.html — tab switcher embedding the 3 individual dashboards as iframes.
Zero data logic here: all computation, live prices, and formatting live in the source pages.
"""

from pathlib import Path
from datetime import date

BASE_DIR = Path(__file__).parent


def latest(pattern: str) -> str:
    reports = sorted((BASE_DIR / "reports").glob(pattern), reverse=True)
    if reports:
        return f"reports/{reports[0].name}"
    # fallback to index redirects
    fallbacks = {
        "*.html":          "index.html",
        "swing_*.html":    "swing_index.html",
        "intraday_*.html": "intraday_index.html",
    }
    return fallbacks.get(pattern, "index.html")


def build_consolidated():
    today_str     = date.today().isoformat()
    screener_url  = latest("*.html").replace("reports/", "reports/") if not latest("*.html").startswith("reports/swing") and not latest("*.html").startswith("reports/intraday") else "index.html"
    swing_url     = latest("swing_*.html")
    intraday_url  = latest("intraday_*.html")

    # screener reports match [0-9]*.html — avoid accidentally picking paper_/intraday_
    screener_reports = sorted(
        (r for r in (BASE_DIR / "reports").glob("*.html")
         if not r.name.startswith("swing_") and not r.name.startswith("intraday_")),
        reverse=True
    )
    screener_url = f"reports/{screener_reports[0].name}" if screener_reports else "index.html"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Dashboard — {today_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; }}
    #tabs {{ display: flex; gap: 0; background: #1a237e; padding: 0 16px; align-items: stretch; }}
    .tab {{ padding: 12px 22px; color: rgba(255,255,255,.65); cursor: pointer; font-size: 14px;
            font-weight: 600; border-bottom: 3px solid transparent; white-space: nowrap;
            transition: color .15s, border-color .15s; user-select: none; }}
    .tab:hover  {{ color: white; }}
    .tab.active {{ color: white; border-bottom-color: white; }}
    #frames {{ position: absolute; top: 43px; left: 0; right: 0; bottom: 0; }}
    iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
              border: none; display: none; background: white; }}
    iframe.active {{ display: block; }}
  </style>
</head>
<body>
  <div id="tabs">
    <div class="tab active" data-frame="screener">📊 Factor Screener</div>
    <div class="tab"        data-frame="swing">📈 Swing Trader</div>
    <div class="tab"        data-frame="intraday">⚡ Intraday Trader</div>
  </div>
  <div id="frames">
    <iframe id="screener" class="active" src="{screener_url}"></iframe>
    <iframe id="swing"    src="{swing_url}"></iframe>
    <iframe id="intraday" src="{intraday_url}"></iframe>
  </div>
  <script>
    document.querySelectorAll('.tab').forEach(tab => {{
      tab.addEventListener('click', () => {{
        document.querySelectorAll('.tab, iframe').forEach(el => el.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.frame).classList.add('active');
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
