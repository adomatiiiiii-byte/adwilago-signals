import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_RELATIVE_PATH = "vendor/lightweight-charts/4.2.0/lightweight-charts.standalone.production.js"
CHART_FILE = PROJECT_ROOT / "static" / CHART_RELATIVE_PATH
EXPECTED_SHA256 = "46FC69534EC098F095BBCD1D9A26D693D39A8B9EEFF7343536765B3DD28C2BDF"
LOCAL_ORIGIN = "http://127.0.0.1:8080"
TEST_PASSWORD = "isolated-auth-test-password-2026"


def _login(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"csrf_token": token, "password": TEST_PASSWORD},
        headers={"origin": LOCAL_ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_dashboard_uses_local_versioned_chart_asset(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        _login(client)
        response = client.get("/")

    assert response.status_code == 200
    assert f"/static/{CHART_RELATIVE_PATH}" in response.text
    assert "unpkg.com/lightweight-charts" not in response.text


def test_local_chart_asset_matches_verified_sha256():
    assert CHART_FILE.is_file()
    assert hashlib.sha256(CHART_FILE.read_bytes()).hexdigest().upper() == EXPECTED_SHA256


def test_chart_asset_requires_authentication_and_preserves_security_headers(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        denied = client.get(f"/static/{CHART_RELATIVE_PATH}")
        assert denied.status_code == 401

        _login(client)
        response = client.get(f"/static/{CHART_RELATIVE_PATH}")

    assert response.status_code == 200
    assert response.content == CHART_FILE.read_bytes()
    assert response.headers["content-type"].split(";", 1)[0] in {
        "application/javascript",
        "text/javascript",
    }
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
