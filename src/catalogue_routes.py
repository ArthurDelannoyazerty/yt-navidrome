"""User-scoped read APIs for the shared public MusicBrainz cache."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal
from pydantic import BaseModel, Field
from fastapi import Query

from album_management import AlbumManager
from catalogue import Catalogue, ENTITY_KINDS
from language_jobs import (preview as language_preview, queue as queue_languages,
                           preview_progress, queue_progress, canary_status, verify_canary,
                           review_state, batch_status)


class SyncRequest(BaseModel):
    user_id: str
    kind: str
    id: str
    sections: list[str] | None = None
    force: bool = False


class AlbumSelection(BaseModel):
    user_id: str
    release_id: str
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    track_ids: list[str] = Field(default_factory=list, max_length=500)


class AlbumApply(BaseModel):
    user_id: str
    preview_token: str


class AlbumUndo(BaseModel):
    user_id: str
    change_id: str


class PreferenceInput(BaseModel):
    user_id: str
    source_id: str
    release_id: str | None = None
    enabled: bool = False
    clear: bool = False


class IndexLibraryInput(BaseModel):
    user_id: str
    limit: int = Field(default=500, ge=1, le=5000)


class LanguageApplyInput(BaseModel):
    user_id: str
    limit: int = Field(default=3, ge=1, le=5000)
    confirmed: Literal[True]
    include_localized: bool = False
    review_token: str | None = Field(default=None, min_length=32, max_length=32)
    scope: Literal["names", "names_and_originals"] = "names_and_originals"


class LanguageUserInput(BaseModel):
    user_id: str
    confirmed: Literal[True]


async def cache_worker(catalogue, stopping):
    """One paginated task at a time; shared HTTP limiter handles 1.1s pacing."""
    while not stopping.is_set():
        scope = await asyncio.to_thread(catalogue.claim)
        if not scope:
            await asyncio.sleep(2)
            continue
        try:
            await asyncio.to_thread(catalogue.process_page, scope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(catalogue.fail, scope, exc)
            logging.warning("MusicBrainz catalogue sync deferred: %s", exc)
        await asyncio.sleep(0.1)


def install_catalogue_routes(app, store, pipeline):
    catalogue = Catalogue(store)
    albums = AlbumManager(store, catalogue)
    app.state.catalogue = catalogue
    app.state.album_manager = albums
    pipeline.album_manager = albums

    @app.get("/api/catalogue/search")
    async def search(user_id: str, kind: str = "artist", q: str = Query("", max_length=180),
                     online: bool = False, local_only: bool = False):
        store.require_user(user_id)
        return await asyncio.to_thread(catalogue.search, kind, q, remote=online,
                                       user_id=user_id, local_only=local_only)

    @app.get("/api/catalogue/entity")
    def entity(user_id: str, kind: str, id: str):
        store.require_user(user_id)
        return catalogue.get(kind, id)

    @app.post("/api/catalogue/sync")
    def sync(data: SyncRequest):
        store.require_user(data.user_id)
        return catalogue.enqueue(data.kind, data.id, data.sections, force=data.force)

    @app.get("/api/catalogue/progress")
    def progress(user_id: str):
        store.require_user(user_id)
        with catalogue.db() as con:
            states = con.execute("SELECT state,COUNT(*) AS count FROM scopes GROUP BY state").fetchall()
            recent = con.execute("""SELECT kind,entity_id,section,state,cursor,total,last_complete_at,
                 last_error FROM scopes WHERE state IN ('PENDING','RUNNING')
                 ORDER BY requested_at LIMIT 25""").fetchall()
            return {"counts": {r["state"]: r["count"] for r in states},
                    "active": [dict(r) for r in recent]}

    @app.post("/api/catalogue/index-library")
    def index_library(data: IndexLibraryInput):
        store.require_user(data.user_id)
        rows = store.rows("""SELECT DISTINCT mbid FROM tracks WHERE user_id=? AND mbid IS NOT NULL
                           ORDER BY mbid LIMIT ?""", (data.user_id, data.limit))
        result = catalogue.index_recordings(row["mbid"] for row in rows)
        return {**result, "queued_or_cached": len(rows),
                "message": "Indexing runs in the background and respects MusicBrainz's shared limiter."}

    @app.get("/api/catalogue/language-preview")
    async def preview_language(user_id: str, include_localized: bool = False):
        store.require_user(user_id)
        return await asyncio.to_thread(language_preview, store, user_id, include_localized)

    @app.get("/api/catalogue/language-progress")
    def language_progress(user_id: str):
        store.require_user(user_id)
        return preview_progress.get(user_id)

    @app.get("/api/catalogue/language-review-state")
    def language_review_state(user_id: str):
        return review_state(store, user_id)

    @app.get("/api/catalogue/language-batch-status")
    def language_batch_status(user_id: str):
        return batch_status(store, user_id)

    @app.get("/api/catalogue/language-apply-progress")
    def language_apply_progress(user_id: str):
        store.require_user(user_id)
        return queue_progress.get(user_id)

    @app.get("/api/catalogue/language-canary")
    def language_canary(user_id: str):
        return canary_status(store, user_id)

    @app.post("/api/catalogue/language-canary-verify")
    async def language_canary_verify(data: LanguageUserInput):
        albums.require_migration_complete()
        store.require_user(data.user_id)
        return await asyncio.to_thread(verify_canary, store, data.user_id)

    @app.post("/api/catalogue/language-apply")
    async def apply_language(data: LanguageApplyInput):
        albums.require_migration_complete()
        store.require_user(data.user_id)
        return await asyncio.to_thread(queue_languages, store, data.user_id, data.limit,
                                       data.include_localized, data.review_token, data.scope)

    @app.get("/api/catalogue/graph")
    def graph(user_id: str, view: str = "downloaded", kinds: str = "artist,recording,release,release-group,work",
              q: str = Query("", max_length=100), focus_id: str | None = None,
              limit: int = Query(180, ge=1, le=180)):
        requested = kinds.split(",") if kinds else []
        if any(kind not in ENTITY_KINDS for kind in requested):
            raise ValueError("Invalid graph node type")
        return catalogue.graph(user_id, view=view, kinds=requested, query=q,
                               focus_id=focus_id, limit=limit)

    @app.get("/api/catalogue/sources")
    def sources(user_id: str):
        store.require_user(user_id)
        return store.rows("SELECT id,title,status,is_playlist FROM sources WHERE user_id=? ORDER BY title", (user_id,))

    @app.post("/api/catalogue/recommendations")
    def recommendations(data: AlbumSelection):
        return catalogue.album_recommendations(data.user_id, data.source_ids, data.track_ids)

    @app.post("/api/catalogue/album-preview")
    def preview(data: AlbumSelection):
        return albums.preview(data.user_id, data.release_id, data.source_ids, data.track_ids)

    @app.post("/api/catalogue/album-apply")
    def apply(data: AlbumApply):
        return albums.apply(data.user_id, data.preview_token)

    @app.post("/api/catalogue/album-retry")
    def retry(data: AlbumUndo):
        return albums.retry(data.user_id, data.change_id)

    @app.get("/api/catalogue/album-history")
    def history(user_id: str):
        return albums.history(user_id)

    @app.post("/api/catalogue/album-undo")
    def undo(data: AlbumUndo):
        return albums.undo(data.user_id, data.change_id)

    @app.post("/api/catalogue/preference")
    def preference(data: PreferenceInput):
        return albums.preference(data.user_id, data.source_id, data.release_id,
                                 enabled=data.enabled, clear=data.clear)

    @app.get("/api/catalogue/preferences")
    def preferences(user_id: str):
        return albums.preferences(user_id)

    return catalogue, albums
