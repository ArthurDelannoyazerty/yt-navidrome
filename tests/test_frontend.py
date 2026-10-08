import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_dom_contract_and_toolbar_layout():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()
    css = (ROOT / "src/static/style.css").read_text()

    html_ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(html_ids) == len(set(html_ids)), "HTML ids must be unique"
    referenced = set(re.findall(r'\$\("([^"]+)"\)', javascript))
    assert referenced <= set(html_ids), sorted(referenced - set(html_ids))

    assert 'id="search"' in html and 'id="filter"' in html
    assert 'id="prev"' in html and 'id="next"' in html and 'id="page"' in html
    assert 'id="clearLogs"' in html
    assert "Stored server logs are retained" in html
    assert "onclick=" not in html

    # Search and filter share one equal-width grid definition and control height.
    assert (
        "grid-template-columns:minmax(220px,1fr) minmax(220px,1fr) auto"
        in css
    )
    assert ".control-field input,.control-field select{height:42px" in css


def test_frontend_exposes_low_friction_navigation_and_actions():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()
    for tab in ("music", "sources", "activity", "maintenance"):
        assert f'data-tab="{tab}"' in html
        assert f'data-panel="{tab}"' in html
    assert 'button("Reprocess"' in javascript
    assert 'button("Redownload"' in javascript
    assert 'button("Delete local copy"' in javascript
    assert 'button("Delete and ignore"' in javascript
    assert 'await api("/api/integrity/run"' in javascript
