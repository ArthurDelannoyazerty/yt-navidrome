"""Shared, literal search/filter clauses for the library and review queue."""

def track_where(user: str, status: str = "ALL", query: str = ""):
    clauses, params = ["t.user_id=?"], [user]
    search = query.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(t.title LIKE ? ESCAPE '\\' OR COALESCE(t.matched_title,'') LIKE ? ESCAPE '\\' "
            "OR EXISTS(SELECT 1 FROM track_origins so WHERE so.track_id=t.id AND "
            "(so.title LIKE ? ESCAPE '\\' OR COALESCE(so.channel,'') LIKE ? ESCAPE '\\')))"
        )
        params.extend((pattern, pattern, pattern, pattern))
    filters = {
        "AVAILABLE": "t.health='AVAILABLE'",
        "MISSING": "t.health='MISSING'",
        "UNAVAILABLE": "t.health='UNAVAILABLE'",
        "WORKING": "t.operation_state IN ('QUEUED','RUNNING','DEFERRED')",
        "QUEUED": "t.operation_state='QUEUED'",
        "RUNNING": "t.operation_state='RUNNING'",
        "DEFERRED": "t.operation_state='DEFERRED'",
        "NEEDS_APPROVAL": "t.operation_state='NEEDS_APPROVAL'",
        "FAILED": "t.operation_state='FAILED'",
        "STOPPED": "t.operation_state='STOPPED'",
        "ATTENTION": "(t.health='MISSING' OR t.operation_state IN ('FAILED','STOPPED'))",
        "UNMATCHED": "(t.mbid IS NULL OR t.mbid='')",
        "SOURCE_METADATA": "t.metadata_source='source'",
        "MANUAL_METADATA": "t.metadata_edited=1",
        "UNKNOWN_METADATA": "t.metadata_source='unknown'",
        "SOURCE_YOUTUBE": "EXISTS(SELECT 1 FROM track_origins o WHERE o.track_id=t.id AND o.provider='youtube' AND o.is_discovery=1)",
        "SOURCE_SPOTIFY": "EXISTS(SELECT 1 FROM track_origins o WHERE o.track_id=t.id AND o.provider='spotify' AND o.is_discovery=1)",
        "NEEDS_AUDIO": (
            "t.current_asset_id IS NULL AND EXISTS(SELECT 1 FROM track_origins o "
            "WHERE o.track_id=t.id AND o.provider='spotify') AND NOT EXISTS("
            "SELECT 1 FROM track_origins o WHERE o.track_id=t.id AND o.downloadable=1 AND o.download_enabled=1)"
        ),
        "INTEGRITY": "EXISTS(SELECT 1 FROM integrity_issues i WHERE i.track_id=t.id AND i.resolved_at IS NULL)",
    }
    if status != "ALL":
        if status not in filters:
            raise ValueError("Unknown track filter")
        clauses.append(filters[status])
    return " AND ".join(clauses), params
