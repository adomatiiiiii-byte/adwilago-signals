import asyncio
import re
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.routes import market_routes
from app.security import (
    AuthSettings,
    SESSION_LIFETIME_SECONDS,
    SESSION_COOKIE_NAME,
    LoginThrottle,
    hash_password,
    password_hash_is_valid,
    verify_password,
)
TEST_PASSWORD = "isolated-auth-test-password-2026"
TEST_PHONE_ORIGIN = "https://192.168.160.174:8443"
_DEFAULT_HASH = object()


LOCAL_ORIGIN = "http://127.0.0.1:8080"


@asynccontextmanager
async def no_external_lifespan(_app):
    yield


def make_app(password_hash=_DEFAULT_HASH, *, phone_origin=TEST_PHONE_ORIGIN, ttl=8 * 60 * 60):
    settings = AuthSettings(
        password_hash=hash_password(TEST_PASSWORD) if password_hash is _DEFAULT_HASH else password_hash,
        phone_origin=phone_origin,
        session_lifetime_seconds=ttl,
    )
    return create_app(settings, lifespan_handler=no_external_lifespan)


def login(client: TestClient, password: str = TEST_PASSWORD, origin: str = LOCAL_ORIGIN):
    page = client.get("/login")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return client.post(
        "/login",
        data={"csrf_token": match.group(1), "password": password},
        headers={"origin": origin},
        follow_redirects=False,
    )


def websocket_headers(client: TestClient, origin: str = LOCAL_ORIGIN):
    headers = {"origin": origin, "host": "127.0.0.1:8080"}
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        headers["cookie"] = f"{SESSION_COOKIE_NAME}={session_id}"
    return headers


class FakeMarketService:
    def __init__(self):
        self.refresh_count = 0
        self.queue = asyncio.Queue()

    async def get_snapshot(self, refresh=False):
        self.refresh_count += int(refresh)
        return {
            "status": "connected",
            "market": "USDJPY",
            "timeframe": "M5",
            "chart_candles": [],
        }

    def get_signal_snapshot(self):
        return {"current_signal": None, "signal_history": [], "today_signals": []}

    def start_tick_stream(self):
        return None

    def subscribe_dashboard(self):
        return self.queue

    def unsubscribe_dashboard(self, _queue):
        return None


def test_scrypt_password_hash_is_salted_and_verifies():
    first = hash_password(TEST_PASSWORD)
    second = hash_password(TEST_PASSWORD)
    assert password_hash_is_valid(first)
    assert first != second
    assert verify_password(TEST_PASSWORD, first)
    assert not verify_password("different password", first)
    assert not password_hash_is_valid("not-a-password-hash")


def test_valid_login_sets_opaque_host_only_http_only_strict_cookie(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=TEST_PHONE_ORIGIN) as client:
        response = login(client, origin=TEST_PHONE_ORIGIN)

    assert response.status_code == 303
    cookie_header = response.headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert "SameSite=Strict" in cookie_header
    assert "Secure" in cookie_header
    assert "Path=/" in cookie_header
    assert "Domain=" not in cookie_header
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    token = cookie[SESSION_COOKIE_NAME].value
    assert len(token) >= 40
    assert "{" not in token and "password" not in token.lower()
    assert int(cookie[SESSION_COOKIE_NAME]["max-age"]) == 8 * 60 * 60


def test_localhost_http_cookie_is_not_secure_but_phone_cookie_is(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        local_response = login(client)
    assert local_response.status_code == 303
    assert "Secure" not in local_response.headers["set-cookie"]
    assert "HttpOnly" in local_response.headers["set-cookie"]
    assert "SameSite=Strict" in local_response.headers["set-cookie"]

    with TestClient(dashboard_test_app, base_url=TEST_PHONE_ORIGIN) as client:
        phone_response = login(client, origin=TEST_PHONE_ORIGIN)
    assert "Secure" in phone_response.headers["set-cookie"]


def test_configured_https_phone_host_gets_secure_cookie_even_without_proxy_scheme():
    application = make_app()
    with TestClient(application, base_url=TEST_PHONE_ORIGIN.replace("https://", "http://")) as client:
        response = login(client, origin=TEST_PHONE_ORIGIN)
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]


def test_invalid_credentials_are_generic_and_do_not_echo_password(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        response = login(client, password="incorrect-secret-value")
    assert response.status_code == 401
    assert "Invalid username or password." in response.text
    assert "incorrect-secret-value" not in response.text


def test_login_throttle_limits_repeated_failures(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        responses = [login(client, password="incorrect") for _ in range(5)]
        assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
        locked_out = login(client, password=TEST_PASSWORD)
    assert locked_out.status_code == 429
    assert int(locked_out.headers["retry-after"]) > 0


def test_login_throttle_window_expires():
    clock_value = [10.0]
    throttle = LoginThrottle(clock=lambda: clock_value[0])
    for _ in range(5):
        throttle.record_failure()
    assert throttle.retry_after() == 60
    clock_value[0] += 61
    assert throttle.retry_after() == 0


def test_missing_or_invalid_auth_configuration_fails_closed():
    for password_hash, ttl in ((None, 8 * 60 * 60), ("broken-hash", 8 * 60 * 60),
                               (_DEFAULT_HASH, SESSION_LIFETIME_SECONDS + 1)):
        application = make_app(password_hash, ttl=ttl)
        with TestClient(application, base_url=LOCAL_ORIGIN) as client:
            response = client.get("/login")
        assert response.status_code == 503
        assert "not configured" in response.text


def test_unauthenticated_routes_are_rejected_before_market_refresh(monkeypatch, dashboard_test_app):
    service = FakeMarketService()
    monkeypatch.setattr(market_routes, "service", service)
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        for path in (
            "/api/market-data",
            "/api/signals",
            "/api/signals/1/evidence",
            "/static/css/style.css",
        ):
            assert client.get(path).status_code == 401
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path, follow_redirects=False).status_code == 303
    assert service.refresh_count == 0


def test_authenticated_dashboard_apis_and_docs_still_work(monkeypatch, dashboard_test_app):
    service = FakeMarketService()
    monkeypatch.setattr(market_routes, "service", service)
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        dashboard = client.get("/")
        market = client.get("/api/market-data")
        signals = client.get("/api/signals")
        evidence = client.get("/api/signals/999/evidence")
        docs = client.get("/docs")
        static = client.get("/static/css/style.css")
    assert dashboard.status_code == 200
    assert 'action="/logout"' in dashboard.text
    assert market.status_code == 200 and market.json()["market"] == "USDJPY"
    assert signals.status_code == 200
    assert evidence.status_code == 404
    assert docs.status_code == 200
    assert static.status_code == 200
    assert service.refresh_count == 1
    for response in (dashboard, market, signals, docs, static):
        assert response.headers["cache-control"] == "no-store, private"


def test_logout_revokes_session_and_clears_cookie(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        page = client.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"origin": LOCAL_ORIGIN},
            follow_redirects=False,
        )
        after_logout = client.get("/api/signals")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert after_logout.status_code == 401


def test_logout_requires_csrf_and_origin(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        response = client.post("/logout", data={}, headers={"origin": "https://evil.invalid"})
        still_authenticated = client.get("/")
    assert response.status_code == 403
    assert still_authenticated.status_code == 200


def test_session_expiry_is_enforced_for_http_requests():
    application = make_app(ttl=1)
    with TestClient(application, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        time.sleep(1.05)
        response = client.get("/api/signals")
    assert response.status_code == 401


def test_websocket_requires_session_and_valid_origin(monkeypatch, dashboard_test_app):
    service = FakeMarketService()
    monkeypatch.setattr(market_routes, "service", service)
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market-data", headers=websocket_headers(client)):
                pass
        assert login(client).status_code == 303
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market-data", headers=websocket_headers(client, "https://evil.invalid")):
                pass
        with client.websocket_connect("/ws/market-data", headers=websocket_headers(client)) as websocket:
            payload = websocket.receive_json()
            assert payload["type"] == "snapshot"
            assert payload["market"] == "USDJPY"


def test_logout_closes_existing_websocket(monkeypatch, dashboard_test_app):
    service = FakeMarketService()
    monkeypatch.setattr(market_routes, "service", service)
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        page = client.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with client.websocket_connect("/ws/market-data", headers=websocket_headers(client)) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
            logout = client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"origin": LOCAL_ORIGIN},
                follow_redirects=False,
            )
            assert logout.status_code == 303
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_json()
    assert caught.value.code == 4401


def test_websocket_closes_when_session_expires(monkeypatch):
    service = FakeMarketService()
    monkeypatch.setattr(market_routes, "service", service)
    application = make_app(ttl=1)
    with TestClient(application, base_url=LOCAL_ORIGIN) as client:
        assert login(client).status_code == 303
        with client.websocket_connect("/ws/market-data", headers=websocket_headers(client)) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_json()
    assert caught.value.code == 4401


def test_invalid_host_origin_and_spoofed_forwarding_headers_do_not_bypass_auth(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        spoofed = client.get(
            "/api/signals",
            headers={
                "x-forwarded-for": "127.0.0.1",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "127.0.0.1:8080",
                "forwarded": "for=127.0.0.1;proto=https;host=127.0.0.1:8080",
                "x-authenticated-user": "admin",
            },
        )
        assert spoofed.status_code == 401
        assert login(client).status_code == 303
        rejected_host = client.get("/api/signals", headers={"host": "evil.invalid"})
    assert rejected_host.status_code == 400


def test_login_csrf_and_origin_are_required(dashboard_test_app):
    with TestClient(dashboard_test_app, base_url=LOCAL_ORIGIN) as client:
        page = client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        bad_origin = client.post(
            "/login",
            data={"csrf_token": csrf, "password": TEST_PASSWORD},
            headers={"origin": "https://evil.invalid"},
        )
        missing_csrf = client.post(
            "/login",
            data={"password": TEST_PASSWORD},
            headers={"origin": LOCAL_ORIGIN},
        )
    assert bad_origin.status_code == 403
    assert missing_csrf.status_code == 400
