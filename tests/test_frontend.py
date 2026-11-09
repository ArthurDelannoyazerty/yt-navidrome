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

    # Both paging landmarks are available and labelled independently.
    for target in ("prevBottom", "nextBottom", "pageBottom"):
        assert f'id="{target}"' in html
    assert 'aria-label="Track pages, top"' in html
    assert 'aria-label="Track pages, bottom"' in html
    assert 'scope="col"' in html


def test_frontend_exposes_low_friction_navigation_and_actions():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()
    for tab in ("music", "sources", "activity", "maintenance"):
        assert f'data-tab="{tab}"' in html
        assert f'data-panel="{tab}"' in html
    assert 'button("Reprocess"' in javascript
    assert 'button("Redownload"' in javascript
    assert 'button("Remove…"' in javascript
    assert 'Block this recording and its URLs' in javascript
    assert 'id="removeChoices"' in html
    assert 'Allow recording (keep URLs ignored)' in javascript
    assert 'await api("/api/integrity/run"' in javascript



def test_keyed_dom_replacement_keeps_a_live_insert_cursor():
    javascript = (ROOT / "src/static/app.js").read_text()
    assert "if (cursor === current) cursor = desired;" in javascript



def test_music_table_keeps_origin_urls_visible():
    javascript = (ROOT / "src/static/app.js").read_text()
    css = (ROOT / "src/static/style.css").read_text()
    assert 'const originList = node("details", null, "origin-links");' in javascript
    assert 'node("a", origin.url, "origin-url")' in javascript
    assert 'link.target = "_blank";' in javascript
    assert 'link.rel = "noopener noreferrer";' in javascript
    # Origin links are rendered in renderTrack before the approval/action controls.
    assert javascript.index('const originList = node("details", null, "origin-links");') < javascript.index('if (track.operation_state === "NEEDS_APPROVAL")')
    assert ".origin-links {" in css and ".origin-url {" in css



def test_frontend_exposes_deferred_retry_state():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()
    css = (ROOT / "src/static/style.css").read_text()
    assert '<option value="DEFERRED">Deferred retry</option>' in html
    assert '["QUEUED", "RUNNING", "DEFERRED"]' in javascript
    assert "Retry after" in javascript
    assert ".NEEDS_APPROVAL, .UNAVAILABLE, .DEFERRED" in css



def test_music_view_exposes_current_user_approve_all():
    html = (ROOT / "src/static/index.html").read_text()
    javascript = (ROOT / "src/static/app.js").read_text()

    assert 'id="approveAll"' in html
    assert '>Approve all</button>' in html
    assert '["approveAll", "best"' in javascript
    assert 'across all pages and filters?' in javascript
    assert 'Use the Approval tab to review individual tracks.' in javascript
    assert 'approveAll.disabled = !stats.approval;' in javascript
    assert 'Approve all (${stats.approval})' in javascript
    assert 'await api("/api/batch", {user_id: user, mode})' in javascript


def test_help_and_bulk_maintenance_have_accessible_dom_targets():
    html = (ROOT / "src/static/index.html").read_text()
    for target in ("retryAll", "repairAll", "exportFailures", "resultsSummary", "libraryScope", "issueKind", "issuePrev", "issueNext"):
        assert f'id="{target}"' in html
    assert 'data-tab="help"' in html and 'data-panel="help"' in html
    assert 'aria-label="Music ingestion workflow"' in html
    assert "Two independent states" in html
    assert "Button reference" in html
    assert 'aria-label="Track operation states and transitions"' in html
    assert 'All operation transitions' in html
    assert "Waiting for approval" in html
    assert html.index('/static/ui-state.js?v=') < html.index('/static/app.js?v=')


def test_approval_is_a_page_between_music_and_sources_with_no_old_toolbar():
    html = (ROOT / "src/static/index.html").read_text()
    js = (ROOT / "src/static/metadata-ui.js").read_text()
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    referenced = set(re.findall(r'\$\("([^"]+)"\)', js))
    assert referenced <= html_ids, sorted(referenced - html_ids)
    assert html.index('data-tab="music"') < html.index('data-tab="approval"') < html.index('data-tab="sources"')
    assert '<section id="approvalPanel"' in html
    assert 'data-panel="approval"' in html
    for removed in ("startReview", "reviewSort", "selectReviewPage", "approveSelected", "metadataDialog", "metadataSaveNext"):
        assert f'id="{removed}"' not in html
    assert "showModal" not in js and "keydown" not in js
    for target in ("approvalInfo", "approvalMetadata", "approvalPrevious", "approvalNext", "approvalPicker",
                   "metadataSuggestions", "sourceMetadataApply", "mbResults", "metadataManualPane"):
        assert target in html_ids
    assert html.index('id="metadataSuggestions"') < html.index('id="sourceMetadataApply"') < html.index('id="mbSearchForm"')
    filters = []
    for target in ("filter", "approvalFilter"):
        content = re.search(fr'<select id="{target}">(.*?)</select>', html, re.S).group(1)
        filters.append(re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', content))
    assert filters[0] == filters[1]
    assert '<option value="NEEDS_APPROVAL" selected>' in html
