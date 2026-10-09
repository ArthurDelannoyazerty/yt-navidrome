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
    assert ".track-toolbar .control-field input,.track-toolbar .control-field select{height:42px" in css
    assert "margin-bottom:0" in css


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



def test_keyed_dom_replacement_keeps_a_live_insert_cursor():
    javascript = (ROOT / "src/static/app.js").read_text()
    assert "if (cursor === current) cursor = desired;" in javascript



def test_music_table_keeps_origin_urls_visible():
    javascript = (ROOT / "src/static/app.js").read_text()
    css = (ROOT / "src/static/style.css").read_text()
    assert 'const originList = node("div", null, "origin-links");' in javascript
    assert 'node("a", origin.url, "origin-url")' in javascript
    assert 'link.target = "_blank";' in javascript
    assert 'link.rel = "noopener noreferrer";' in javascript
    # Origin links are rendered in renderTrack before the approval/action controls.
    assert javascript.index('const originList = node("div", null, "origin-links");') < javascript.index('if (track.operation_state === "NEEDS_APPROVAL")')
    assert ".origin-links{" in css and ".origin-url{" in css



def test_frontend_exposes_deferred_retry_state():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()
    css = (ROOT / "src/static/style.css").read_text()
    assert '<option value="DEFERRED">Deferred retry</option>' in html
    assert '["QUEUED", "RUNNING", "DEFERRED"]' in javascript
    assert '["Deferred", stats.deferred]' in javascript
    assert '["Processing", stats.running]' in javascript
    assert "Retry after" in javascript
    assert ".NEEDS_APPROVAL,.UNAVAILABLE,.DEFERRED" in css



def test_music_view_exposes_current_user_approve_all():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()

    assert 'id="approveAll"' in html
    assert '>Approve all</button>' in html
    assert '["approveAll", "best"' in javascript
    assert 'This only affects the current library user.' in javascript
    assert 'approveAll.disabled = !stats.approval;' in javascript
    assert 'Approve all (${stats.approval})' in javascript
    assert 'await api("/api/batch", {user_id: state.user, mode})' in javascript
