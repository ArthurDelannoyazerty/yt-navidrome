import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    import common, store, pipeline, runtime, beets_bridge
    state = tmp_path / "state"
    library = tmp_path / "library"
    state.mkdir()
    library.mkdir()
    monkeypatch.setenv("INGESTOR_STATE_DIR", str(state))
    monkeypatch.setenv("NAVIDROME_LIB_DIR", str(library))
    monkeypatch.setenv("YT_API_KEY", "test-api-key")
    monkeypatch.setenv("YTDLP_UPDATE_ENABLED", "false")
    for module in (common, store, pipeline, runtime, beets_bridge):
        if hasattr(module, "STATE"):
            monkeypatch.setattr(module, "STATE", state)
        if hasattr(module, "LIBRARY"):
            monkeypatch.setattr(module, "LIBRARY", library)
    return state, library


@pytest.fixture
def db(environment):
    from store import Store
    result = Store(environment[0] / "ingestor.sqlite")
    result.init()
    return result


@pytest.fixture
def track(db):
    from providers import Entry, Snapshot, parse_source
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLtest"), "admin")
    db.apply_snapshot(source, Snapshot("A playlist", [Entry("abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk", "Artist - Song", "2021-04-17T21:36:42Z", "membership-1", 0)]))
    db.execute("UPDATE jobs SET state='DONE'")
    return db.one("SELECT * FROM tracks")
