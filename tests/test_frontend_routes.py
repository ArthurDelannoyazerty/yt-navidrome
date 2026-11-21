"""Direct browser routes must not affect API boundaries, cache policy or tenancy."""
from fastapi.testclient import TestClient
from main import create_app

ALBUM = "11111111-1111-4111-8111-111111111111"
EDITION = "22222222-2222-4222-8222-222222222222"


def test_direct_frontend_urls_serve_the_same_csp_protected_shell(db, environment):
    with TestClient(create_app(db, start_workers=False)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "router.js" in root.text
        assert "Music Ingestor" in root.text
        routes = [
            "/libraries/admin/music",
            f"/libraries/admin/music/{ALBUM}",
            "/libraries/admin/music/ignored",
            f"/libraries/admin/approval/{ALBUM}",
            "/libraries/admin/catalogue/recording/" + ALBUM,
            "/libraries/admin/catalogue/release-group/" + ALBUM,
            "/libraries/admin/albums",
            f"/libraries/admin/albums/{ALBUM}",
            f"/libraries/admin/albums/{ALBUM}/editions/{EDITION}",
            "/libraries/admin/graph/artist/" + ALBUM,
            "/libraries/admin/sources/" + ALBUM,
            "/libraries/admin/activity",
            "/libraries/admin/maintenance",
            "/libraries/admin/help/album",
            "/libraries/admin/language",
        ]
        for route in routes:
            response = client.get(route, follow_redirects=False)
            assert response.status_code == 200, route
            assert response.text == root.text, route
            assert "no-store" in response.headers["cache-control"], route
            assert "script-src 'self'" in response.headers["content-security-policy"], route
            assert response.headers["x-content-type-options"] == "nosniff"
        asset = client.get("/static/router.js")
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]
        assert "application" not in client.get("/api/users").headers["content-type"] or "json" in client.get("/api/users").headers["content-type"]


def test_invalid_client_routes_are_not_caught_as_spa_pages(db, environment):
    with TestClient(create_app(db, start_workers=False)) as client:
        for url in (
            "/libraries/admin/nope",
            "/libraries/admin/music/one/two",
            "/libraries/admin/albums/group/wrong/edition",
            "/libraries/admin/catalogue/unknown/item",
            "/libraries/admin/graph/release/not-uuid/extra",
            "/libraries/admin/help/topic/too-deep",
            "/libraries/admin/sources/source-1/extra",
        ):
            result = client.get(url)
            assert result.status_code == 404, url
            assert "text/html" not in result.headers["content-type"], url
