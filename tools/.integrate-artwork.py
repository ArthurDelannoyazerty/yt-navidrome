"""One-time, assertion-checked integration; removed from the feature branch afterward."""
from pathlib import Path


def replace(path, old, new, count=1):
    path = Path(path)
    value = path.read_text()
    if value.count(old) != count:
        raise RuntimeError(f"Unexpected baseline in {path}: {old[:90]!r}")
    path.write_text(value.replace(old, new))


replace("src/store.py", "from audio_tags import REPAIRABLE_ISSUES", "from audio_tags import REPAIRABLE_ISSUES\nfrom artwork import SCHEMA as ARTWORK_SCHEMA, Artwork, transfer_origin")
replace("src/store.py", "SCHEMA_VERSION = 6", "SCHEMA_VERSION = 7")
replace("src/store.py", "if version not in (0, 1, 2, 3, 4, 5, SCHEMA_VERSION):", "if version not in (0, 1, 2, 3, 4, 5, 6, SCHEMA_VERSION):")
replace("src/store.py", "DROP_ORDER = [", 'DROP_ORDER = [\n    "artwork_preferences", "artwork_candidates",')
replace("src/store.py", '            con.executescript(SCHEMA + METADATA_SCHEMA)\n            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")', '''            if version == 6:
                self.backup_database("schema-v6")
            con.executescript("BEGIN IMMEDIATE;\\n" + SCHEMA + METADATA_SCHEMA + ARTWORK_SCHEMA
                              + f"\\nPRAGMA user_version={SCHEMA_VERSION};\\nCOMMIT;")''')
replace("src/store.py", '            con.execute("UPDATE track_origins SET track_id=?,last_error=NULL WHERE id=?", (target_id, origin_id))', '''            con.execute("UPDATE track_origins SET track_id=?,last_error=NULL WHERE id=?", (target_id, origin_id))
            transfer_origin(con, source_id, target_id, origin_id)''')
replace("src/store.py", '                con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (target_id, origin_id))', '''                con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (target_id, origin_id))
                transfer_origin(con, source_id, target_id, origin_id)''')
replace("src/store.py", '                con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (new_id, origin_id))', '''                con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (new_id, origin_id))
                transfer_origin(con, track_id, new_id, origin_id)''')
replace("src/store.py", '            metadata_source = plan.get("metadata_source")', '''            if result.get("artwork"):
                Artwork.record(con, {"id": target_id, "user_id": plan["working_track"]["user_id"]}, result["artwork"])
            metadata_source = plan.get("metadata_source")''')
replace("src/store.py", "pending_operation=CASE WHEN ?='approve' THEN pending_operation ELSE NULL END,", "pending_operation=CASE WHEN ? IN ('approve','artwork') THEN pending_operation ELSE NULL END,")
replace("src/store.py", 'if row["result"].get("reingest_origin_id"):', 'if row["result"].get("reingest_origin_id") or row["result"].get("artwork_pending"):')
replace("src/store.py", 'receipt = {"kind": "DEDUPLICATED", "source_id": source_id, "target_id": target_id,', 'receipt = {"kind": "DEDUPLICATED", "source_id": source_id, "target_id": target_id,\n                       "artwork_pending": True,')
replace("src/pipeline.py", '        mode = payload.get("mode", "ingest")\n        operation', '''        mode = payload.get("mode", "ingest")
        if mode == "artwork":
            from artwork_jobs import process
            await process(self, job)
            return
        operation''')
replace("src/pipeline.py", '        target_track["source_url"] = origin["url"]', '''        target_track["source_url"] = origin["url"]
        target_track["artwork_origin_id"] = origin["id"]
        target_track["artwork_extra_origin_id"] = origin["id"]
        if plan["route"] == "split":
            target_track["artwork_track_id"] = source_track["id"]
            target_track["artwork_split"] = True''')
replace("src/pipeline.py", '        result = receipt.get("result") or {}\n        source_id = result.get("source_id")', '''        from artwork_jobs import queue_merge_followup
        queue_merge_followup(self.store, receipt)
        result = receipt.get("result") or {}
        source_id = result.get("source_id")''')
replace("src/source_metadata.py", '    tags = {}\n    basis =', '    tags = {}\n    pristine = False\n    basis =')
replace("src/source_metadata.py", '        if not processed:\n            for key', '        if not processed:\n            pristine = True\n            for key')
replace("src/source_metadata.py", '    fallback = title_tags(origin["title"])\n    for key, value in fallback.items():', '''    if pristine:
        try:
            from artwork import Artwork
            Artwork(store).capture({"id": origin["track_id"], "user_id": origin["user_id"], "mbid": None}, path, origin=origin)
        except Exception:
            pass  # Optional source artwork cannot prevent retaining source metadata.
    fallback = title_tags(origin["title"])
    for key, value in fallback.items():''')
replace("src/beets_bridge.py", '        # Plugins may write the file again, so force the raw compatibility tags last.', '''        artwork_result = None
        from artwork import Artwork
        service = Artwork(self.store)
        artwork_track = {**track, "id": track.get("artwork_track_id", track["id"]),
                         "mbid": item.mb_trackid or None, "release_id": item.mb_albumid or None}
        try:
            if album and not source_reset:
                origin = self.store.one("SELECT * FROM track_origins WHERE id=? AND user_id=?",
                                        (track.get("artwork_origin_id"), track["user_id"]))
                service.capture(artwork_track, final_path, source_type="coverart", origin=origin)
            artwork_result = service.apply(artwork_track, final_path, source_reset=source_reset)
            warnings.extend(artwork_result.get("warnings", []))
        except Exception as exc:
            preference = service.preference(artwork_track)
            if preference and preference["mode"] == "manual":
                raise ValueError("Pinned artwork could not be restored; existing audio was retained. Repair the artwork selection before retrying.") from exc
            warnings.append(f"Track artwork: {exc}")
        # Plugins may write the file again, so force the raw compatibility tags last.''')
replace("src/beets_bridge.py", '        item.pipeline_complete = "yes"\n        item.store()', '''        item.pipeline_artwork = json.dumps(artwork_result) if artwork_result else ""
        item.pipeline_complete = "yes"
        item.store()''')
replace("src/beets_bridge.py", '            "release_id": item.mb_albumid or None,\n        }', '''            "release_id": item.mb_albumid or None,
            "artwork": json.loads(item.get("pipeline_artwork") or "null"),
        }''')
replace("src/beets_bridge.py", '    if mode == "source_links":', '''    if mode == "artwork":
        from artwork import Artwork
        return Artwork(bridge.store).apply(track, path, selection=request.get("selection"),
            framing=request.get("framing", "original"), refresh=bool(request.get("refresh")))
    if mode == "source_links":''')
replace("src/main.py", 'from review_routes import install_review_routes', 'from review_routes import install_review_routes\nfrom artwork_routes import install_artwork_routes')
replace("src/main.py", '"spotify-ui.js", "metadata-ui.js"):', '"spotify-ui.js", "metadata-ui.js", "artwork-ui.js"):')
replace("src/main.py", '    install_review_routes(app, store)', '    install_review_routes(app, store)\n    install_artwork_routes(app, store)')
replace("src/static/app.js", '  menuItems.append(editMetadata, technicalDetails, refreshSuggestions, repairTags, remove);', '''  const artwork = button("Artwork...", () => window.ArtworkUI.open(track));
  artwork.disabled = busy || track.health !== "AVAILABLE" || !window.ArtworkUI;
  menuItems.append(editMetadata, artwork, technicalDetails, refreshSuggestions, repairTags, remove);''')
replace("src/static/app.js", 'window.MetadataUI?.reset();', 'window.MetadataUI?.reset();\n  window.ArtworkUI?.reset();')
replace("src/static/index.html", '<script src="/static/metadata-ui.js?v=__STATIC_VERSION__" defer></script>', '<script src="/static/metadata-ui.js?v=__STATIC_VERSION__" defer></script>\n<script src="/static/artwork-ui.js?v=__STATIC_VERSION__" defer></script>')
replace("src/static/index.html", '<button id="updateSourceLinks"', '<button id="fillMissingCovers" type="button" class="secondary">Fill missing covers</button>\n          <button id="updateSourceLinks"')
replace("src/static/index.html", '</body>', '''<dialog id="artworkDialog" class="artwork-dialog" aria-labelledby="artworkHeading">
  <div class="dialog-header"><h2 id="artworkHeading">Track artwork</h2><button id="artworkClose" type="button" class="secondary">Close</button></div>
  <p id="artworkTitle" class="dialog-track"></p>
  <div class="artwork-current"><img id="artworkCurrent" alt="Current embedded cover"><span id="artworkCurrentEmpty">No artwork embedded</span><p id="artworkMode" class="hint"></p></div>
  <p class="hint">Each song can have its own cover, even within the same album. Images are never stretched. A manual selection stays pinned until you choose Auto-select.</p>
  <div class="row artwork-tools"><button id="artworkRefresh" type="button" class="secondary">Find covers</button><button id="artworkAuto" type="button" class="secondary">Auto-select</button><label class="artwork-upload">Upload cover<input id="artworkUpload" type="file" accept="image/jpeg,image/png,image/webp"></label></div>
  <p id="artworkStatus" role="status" aria-live="polite"></p>
  <div id="artworkCandidates" class="artwork-grid" role="group" aria-label="Available cover images"></div>
  <section id="artworkPreviewPanel" class="artwork-preview" hidden>
    <div><h3>Selected image preview</h3><p id="artworkPreviewCaption" class="hint"></p><label>Framing<select id="artworkFraming"><option value="original">Original proportions</option><option value="trim">Trim detected uniform border</option><option value="fit">Fit inside a square (no cropping)</option><option value="crop">Center crop to square</option></select></label><p class="hint">Framing affects this track only. The uncropped image remains cached.</p><button id="artworkApply" type="button" disabled>Use this image</button></div>
    <img id="artworkPreview" alt="Selected image preview">
  </section>
</dialog>

</body>''')
css = Path("src/static/style.css")
css.write_text(css.read_text() + '''
/* Individual track covers; original proportions are retained throughout. */
.artwork-dialog { width: min(900px, 94vw); max-height: 92vh; overflow: auto; }
.artwork-current { display: flex; gap: 1rem; align-items: center; margin-block: 1rem; }
.artwork-current img { width: 96px; height: 96px; object-fit: contain; border-radius: 6px; }
.artwork-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; }
.artwork-card { border: 1px solid var(--border, #d6dfda); border-radius: 8px; padding: .75rem; overflow-wrap: anywhere; min-width: 0; }
.artwork-card label { cursor: pointer; display: block; }
.artwork-card img { display: block; width: 100%; height: 160px; object-fit: contain; margin-bottom: .5rem; }
.artwork-card input { width: auto; margin-right: .5rem; }
.artwork-card small, .artwork-card a { display: block; margin-top: .4rem; }
.artwork-preview { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; padding: 1rem; border-top: 1px solid var(--border, #d6dfda); }
.artwork-preview img { max-width: 100%; max-height: 320px; object-fit: contain; justify-self: center; }
.artwork-upload, .artwork-upload input { max-width: 100%; }
.artwork-upload input { display: block; font-size: .85rem; }
.artwork-tools { flex-wrap: wrap; align-items: end; }
.artwork-preview[hidden], .artwork-current img[hidden], .artwork-current span[hidden] { display: none; }
@media (max-width: 600px) {
  .artwork-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem; }
  .artwork-card { padding: .5rem; }
  .artwork-card img { height: 120px; }
  .artwork-preview { grid-template-columns: 1fr; padding: .5rem 0; }
  .artwork-current { align-items: flex-start; }
}
''')
replace("tests/test_temporary_audio.py", 'def listening(db, track, environment, monkeypatch):\n', 'def listening(db, track, environment, monkeypatch):\n    # Offline lifecycle test: no live thumbnail traffic.\n    monkeypatch.setenv("YT_API_KEY", "")\n')
print("Artwork integration applied")
