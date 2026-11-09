from pathlib import Path
root = Path.cwd()
def edit(path, old, new, count=1):
    p = root/path; value = p.read_text()
    if value.count(old) != count: raise RuntimeError(f'{path}: unexpected input for {old[:80]!r}')
    p.write_text(value.replace(old, new))
# Public source URLs are distinct from signed playback/thumbnail URLs.
edit('src/common.py', 'from zoneinfo import ZoneInfo', 'from zoneinfo import ZoneInfo\nfrom urllib.parse import parse_qs, urlsplit')
edit('src/common.py', '\ndef user_name(', '''
def canonical_source_urls(urls) -> list[str]:
    """Stable public track URLs only; discard credentials and tracking queries."""
    result = set()
    for value in urls or []:
        try:
            parts = urlsplit(str(value))
            if parts.scheme not in {"https", "http"} or parts.username or parts.password:
                continue
            if parts.port not in {None, 80, 443}:
                continue
            host, identity = (parts.hostname or "").lower(), None
            if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}:
                if host == "youtu.be":
                    identity = parts.path.strip("/")
                elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
                    identity = parts.path.split("/")[2]
                elif parts.path == "/watch":
                    identity = parse_qs(parts.query).get("v", [""])[0]
                if identity and re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
                    result.add("https://www.youtube.com/watch?v=" + identity)
            elif host == "open.spotify.com":
                match = re.fullmatch(r"/(?:intl-[A-Za-z-]+/)?track/([A-Za-z0-9]{22})/?", parts.path)
                if match:
                    result.add("https://open.spotify.com/track/" + match[1])
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(result, key=lambda url: ("spotify.com" in url, url))


def music_comment(value: str | None, source_urls=()) -> str:
    """Preserve the discovery line and append the portable source provenance."""
    comment = discovery_comment(value)
    urls = canonical_source_urls(source_urls)
    if urls:
        comment += "\\n\\nSources:\\n" + "\\n".join(urls)
    return comment


def user_name(''')
edit('src/audio_tags.py', 'from common import discovery_comment', 'from common import discovery_comment, music_comment')
edit('src/audio_tags.py', '"DISCOVERY_COMMENT_MISMATCH", "LOUDNESS_MISSING",', '"DISCOVERY_COMMENT_MISMATCH", "SOURCE_COMMENT_MISMATCH", "LOUDNESS_MISSING",')
edit('src/audio_tags.py', 'def inspect_tags(path: str | Path, discovered_at: str | None) -> dict:', 'def inspect_tags(path: str | Path, discovered_at: str | None, source_urls=None) -> dict:')
edit('src/audio_tags.py', '"expected_discovery": expected, "comment": None, "description": None,', '"expected_discovery": expected, "expected_comment": music_comment(discovered_at, source_urls),\n        "comment": None, "description": None,')
edit('src/audio_tags.py', '    if result["comment"] != expected or result["description"] != expected:\n        result["issues"].append("DISCOVERY_COMMENT_MISMATCH")', '''    first_line = lambda value: (value or "").split("\\n", 1)[0]
    if any(first_line(result[key]) != expected for key in ("comment", "description")):
        result["issues"].append("DISCOVERY_COMMENT_MISMATCH")
    if source_urls is not None and any(result[key] != result["expected_comment"] for key in ("comment", "description")):
        result["issues"].append("SOURCE_COMMENT_MISMATCH")''')
edit('src/beets_bridge.py', 'atomic_json, discovery_comment, user_name', 'atomic_json, music_comment, canonical_source_urls, user_name')
edit('src/beets_bridge.py', 'def write_discovery_tags(path: str | Path, value: str | None):', 'def write_discovery_tags(path: str | Path, value: str | None, source_urls=()):')
edit('src/beets_bridge.py', '        comment = discovery_comment(value)', '        comment = music_comment(value, source_urls)')
edit('src/beets_bridge.py', '    def delete(self, track, *, delete_file=True):', '''    def source_urls(self, track):
        if "source_urls" in track:
            return canonical_source_urls(track["source_urls"])
        origins = self.store.origins_for_track(track["id"], track["user_id"]) if hasattr(self, "store") else []
        return canonical_source_urls([origin["url"] for origin in origins] + [track.get("source_url", "")])

    def delete(self, track, *, delete_file=True):''')
for path in ['staged', 'path', 'os.fsdecode(existing.path)', 'final_path']:
    before = f'self.write_discovery_tags({path}, track.get("discovered_at"))'
    p=root/'src/beets_bridge.py'; value=p.read_text()
    if before in value: p.write_text(value.replace(before, f'self.write_discovery_tags({path}, track.get("discovered_at"), self.source_urls(track))'))
edit('src/beets_bridge.py', 'existing.comments = discovery_comment(track.get("discovered_at"))', 'existing.comments = music_comment(track.get("discovered_at"), self.source_urls(track))')
edit('src/beets_bridge.py', 'item.comments = discovery_comment(track["discovered_at"])', 'item.comments = music_comment(track["discovered_at"], self.source_urls(track))')
edit('src/beets_bridge.py', '    if mode == "repair":\n        return bridge.repair_file(track, path)', '''    if mode == "source_links":
        bridge.write_discovery_tags(path, track.get("discovered_at"), bridge.source_urls(track))
        return {}
    if mode == "repair":
        return bridge.repair_file(track, path)''')
edit('src/store.py', 'SCHEMA_VERSION = 5', 'SCHEMA_VERSION = 6')
edit('src/store.py', ' temporary_candidate_sha256 TEXT,\n created_at', " temporary_candidate_sha256 TEXT,\n metadata_source TEXT NOT NULL DEFAULT 'unknown',\n metadata_edited INTEGER NOT NULL DEFAULT 0,\n metadata_reviewed_at TEXT,\n created_at")
edit('src/store.py', 'if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):', 'if version not in (0, 1, 2, 3, 4, 5, SCHEMA_VERSION):')
edit('src/store.py', '            con.executescript(SCHEMA + METADATA_SCHEMA)', '''            if version in (1, 2, 3, 4, 5):
                self.backup_database("schema-v5")
                con.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in con.execute("PRAGMA table_info(tracks)")}
                for name, definition in (
                    ("metadata_source", "TEXT NOT NULL DEFAULT 'unknown'"),
                    ("metadata_edited", "INTEGER NOT NULL DEFAULT 0"),
                    ("metadata_reviewed_at", "TEXT"),
                ):
                    if name not in columns:
                        con.execute(f"ALTER TABLE tracks ADD COLUMN {name} {definition}")
                # Missing identity alone is not proof that source metadata was chosen.
                con.execute("UPDATE tracks SET metadata_source='musicbrainz' WHERE mbid IS NOT NULL AND mbid<>'' AND metadata_source='unknown'")
                con.execute("UPDATE tracks SET metadata_source='pending' WHERE temporary_candidate_sha256 IS NOT NULL AND metadata_source='unknown'")
                con.execute("""UPDATE tracks SET metadata_source='source' WHERE metadata_source='unknown'
                    AND EXISTS(SELECT 1 FROM operation_receipts r WHERE r.target_track_id=tracks.id
                        AND r.action='RESTORE_SOURCE' AND r.finalized_at IS NOT NULL
                        AND NOT EXISTS(SELECT 1 FROM operation_receipts newer
                            WHERE newer.target_track_id=tracks.id AND newer.rowid>r.rowid
                            AND newer.action NOT IN ('COMMENT','REPAIR','SOURCE_LINKS','ARTWORK')))""")
                con.execute("PRAGMA user_version=6")
                con.commit()

            con.executescript(SCHEMA + METADATA_SCHEMA)''')
edit('src/store.py', 'elif mode == "repair":\n                if track["health"]', 'elif mode in {"repair", "source_links"}:\n                if track["health"]')
edit('src/store.py', 'if mode not in {"retry", "repair", "best", "original"}:', 'if mode not in {"retry", "repair", "source_links", "best", "original"}:')
edit('src/store.py', '        if mode == "repair":\n            tracks = self.repair_candidates(user)', '''        if mode == "source_links":
            tracks = self.rows("SELECT id FROM tracks WHERE user_id=? AND health='AVAILABLE' AND operation_state='IDLE'", (user,))
        elif mode == "repair":
            tracks = self.repair_candidates(user)''')
edit('src/store.py', "temporary_candidate_sha256=?,health='AVAILABLE',updated_at=? WHERE id=?", "temporary_candidate_sha256=?,health='AVAILABLE',metadata_source='pending',updated_at=? WHERE id=?")
edit('src/store.py', '            if reason not in {"retag", "restore_source"}:\n                con.execute', '''            metadata_source = plan.get("metadata_source") or ("musicbrainz" if result.get("mbid") else "source" if reason == "restore_source" else plan["working_track"].get("metadata_source", "unknown"))
            con.execute("UPDATE tracks SET metadata_source=?,metadata_edited=?,metadata_reviewed_at=? WHERE id=?",
                        (metadata_source, int(plan.get("metadata_edited", False)), now, target_id))
            if reason not in {"retag", "restore_source"}:
                con.execute''')
edit('src/store.py', '            if changed and track["health"] == "AVAILABLE" and track["current_asset_id"]:', '            if track["health"] == "AVAILABLE" and track["current_asset_id"]:')
edit('src/store.py', 'def commit_repair(self, track_id, asset_id, fingerprint, beets_id, operation_id):', 'def commit_repair(self, track_id, asset_id, fingerprint, beets_id, operation_id, action="repair"):')
edit('src/store.py', '            placeholders = ",".join("?" for _ in REPAIRABLE_ISSUES)\n            con.execute(\n                f"UPDATE integrity_issues', '''            resolved_kinds = REPAIRABLE_ISSUES if action == "repair" else ("DISCOVERY_COMMENT_MISMATCH", "SOURCE_COMMENT_MISMATCH")
            placeholders = ",".join("?" for _ in resolved_kinds)
            con.execute(
                f"UPDATE integrity_issues''')
edit('src/store.py', '(utcnow(), track_id, *REPAIRABLE_ISSUES),', '(utcnow(), track_id, *resolved_kinds),')
edit('src/store.py', '"repair", {"kind": "REPAIRED", "target_id": track_id}, finalized=True)', 'action, {"kind": "REPAIRED", "target_id": track_id}, finalized=True)')
edit('src/library_query.py', '        "UNMATCHED": "(t.mbid IS NULL OR t.mbid=\'\')",', '''        "UNMATCHED": "(t.mbid IS NULL OR t.mbid='')",
        "SOURCE_METADATA": "t.metadata_source='source'",
        "MANUAL_METADATA": "t.metadata_edited=1",
        "UNKNOWN_METADATA": "t.metadata_source='unknown'",''')
edit('src/pipeline.py', '    discovery_comment, next_nightly, safe_name, sha256_file, utcnow,', '    discovery_comment, canonical_source_urls, next_nightly, safe_name, sha256_file, utcnow,')
edit('src/pipeline.py', '    async def bridge(self, job, track, directory, mode, **kwargs):\n        return', '''    async def bridge(self, job, track, directory, mode, **kwargs):
        if "source_urls" not in track:
            track = {**track, "source_urls": canonical_source_urls([
                origin["url"] for origin in self.store.origins_for_track(track["id"], track["user_id"])
            ] + [track.get("source_url", "")])}
        return''')
edit('src/pipeline.py', '            plan["action"] = action\n            atomic_json(plan_path', '''            plan["action"] = action
            if selected and selected.get("kind") == "source":
                plan["metadata_source"], plan["metadata_edited"] = "source", bool(overrides)
            elif selected and not selected.get("asis"):
                plan["metadata_source"], plan["metadata_edited"] = "musicbrainz", bool(set(overrides or {}) & {"artist", "title", "album"})
            else:
                plan["metadata_source"] = source_track.get("metadata_source", "unknown")
                plan["metadata_edited"] = bool(source_track.get("metadata_edited") or overrides)
            atomic_json(plan_path''')
edit('src/pipeline.py', '        target_track["source_url"] = origin["url"]\n        target_asset', '''        target_track["source_url"] = origin["url"]
        target_track["source_urls"] = canonical_source_urls(
            ([o["url"] for o in self.store.origins_for_track(target_track["id"], target_track["user_id"])]
             if plan["route"] != "split" else []) + [origin["url"]]
        )
        target_asset''')
edit('src/pipeline.py', 'async def repair_tags(self, job, track, directory, operation):', 'async def repair_tags(self, job, track, directory, operation, mode="repair"):')
edit('src/pipeline.py', 'await self.bridge(job, track, directory, "repair", path=str(staged))', 'await self.bridge(job, track, directory, mode, path=str(staged))')
edit('src/pipeline.py', 'refreshed.get("beets_id"), operation)', 'refreshed.get("beets_id"), operation, action=mode)')
edit('src/pipeline.py', '        if mode == "repair":\n            await self.repair_tags(job, track, directory, operation)', '        if mode in {"repair", "source_links"}:\n            await self.repair_tags(job, track, directory, operation, mode)')
edit('src/pipeline.py', '        self.report(job)("INFO", "Repaired discovery and loudness tags without redownloading or changing identity")', '        self.report(job)("INFO", "Updated source links without downloading or re-encoding" if mode == "source_links" else "Repaired discovery and loudness tags without redownloading or changing identity")')
edit('src/pipeline.py', 'tags = await asyncio.to_thread(inspect_tags, path, track.get("discovered_at"))', 'tags = await asyncio.to_thread(inspect_tags, path, track.get("discovered_at"), [o["url"] for o in self.store.origins_for_track(track["id"], user)])')
edit('src/pipeline.py', '                "LOUDNESS_MISSING": "Opus gain', '                "SOURCE_COMMENT_MISMATCH": "COMMENT or DESCRIPTION needs the current source URLs.",\n                "LOUDNESS_MISSING": "Opus gain')
edit('src/pipeline.py', '        old_assets = list(result.get("old_assets") or [])', '''        if source_id and source_id != target_id and not result.get("source_orphan"):
            remaining = self.store.one("SELECT * FROM tracks WHERE id=?", (source_id,))
            if remaining and remaining.get("file_path") and Path(remaining["file_path"]).is_file():
                comment_dir = STATE / "staging" / f"comment-{source_id}"
                comment_dir.mkdir(parents=True, exist_ok=True)
                await self.bridge(job, remaining, comment_dir, "comment", path=remaining["file_path"])
                await self.refresh_asset_fingerprint(source_id)
                shutil.rmtree(comment_dir, ignore_errors=True)
        old_assets = list(result.get("old_assets") or [])''')
edit('src/main.py', 'details["file_tags"] = inspect_tags(path, details.get("discovered_at"))', 'details["file_tags"] = inspect_tags(path, details.get("discovered_at"), [o["url"] for o in details["origins"]])')
edit('src/main.py', 'class UserInput(BaseModel):', '''class SourceMonitoringInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    monitored: bool


class UserInput(BaseModel):''')
edit('src/main.py', '    @app.delete("/api/sources/{source_id}")', '''    @app.post("/api/sources/{source_id}/monitoring")
    def source_monitoring(source_id: str, data: SourceMonitoringInput):
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            source = con.execute("SELECT * FROM sources WHERE id=? AND user_id=?", (source_id, data.user_id)).fetchone()
            if not source:
                raise LookupError("Source not found for this user")
            if not source["is_playlist"] or source["provider"] == "legacy":
                raise ValueError("Monitoring is available for provider playlists and collections")
            con.execute("UPDATE sources SET monitored=? WHERE id=?", (int(data.monitored), source_id))
            if data.monitored:
                store.enqueue("sync", source_id, data.user_id, con=con)
        return {"message": "Monitoring resumed; sync queued" if data.monitored else "Monitoring paused; music and playlist history retained. A sync already running may finish."}

    @app.delete("/api/sources/{source_id}")''')
edit('src/static/index.html', '          <option value="UNMATCHED">Unmatched identity</option>', '''          <option value="UNMATCHED">Unmatched identity</option>
          <option value="SOURCE_METADATA">Source metadata applied</option>
          <option value="MANUAL_METADATA">Manually edited metadata</option>
          <option value="UNKNOWN_METADATA">Metadata origin unknown (older imports)</option>''')
edit('src/static/index.html', '<button id="repairAll"', '<button id="updateSourceLinks" type="button" class="secondary">Update file source links</button>\n          <button id="repairAll"')
edit('src/static/app.js', '  controls.append(button("Remove", async () => {', '''  if (source.is_playlist && source.provider !== "legacy") {
    const monitor = button(source.monitored ? "Pause monitoring" : "Resume monitoring", async () => {
      const user = state.user;
      await api(`/api/sources/${encodeURIComponent(source.id)}/monitoring`, {user_id: user, monitored: !source.monitored});
      if (user !== state.user) return;
      notice(source.monitored ? "Monitoring paused. Music and playlist history kept." : "Monitoring resumed.");
      await loadSources(state.generation, false);
    }, "secondary");
    controls.append(monitor);
  }
  controls.append(button("Remove", async () => {''')
edit('src/static/app.js', '  ["repairAll", "repair",', '  ["updateSourceLinks", "source_links", () => `Write source URLs into COMMENT and DESCRIPTION for idle playable tracks in "${state.user}"? Discovery dates, audio, artwork and metadata are preserved. No downloads or loudness analysis.`],\n  ["repairAll", "repair",')
edit('src/static/app.js', '["Expected discovery", values.expected_discovery]', '["Expected comment", values.expected_comment || values.expected_discovery]')
