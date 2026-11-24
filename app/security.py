"""Single-user dashboard authentication with server-side sessions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from dotenv import load_dotenv


SESSION_COOKIE_NAME = "adwilago_session"
SESSION_LIFETIME_SECONDS = 8 * 60 * 60
LOGIN_CSRF_LIFETIME_SECONDS = 10 * 60
LOGIN_CSRF_MAX_TOKENS = 512
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 60
MAX_PASSWORD_BYTES = 1024
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
LOCAL_ORIGINS = frozenset({"http://127.0.0.1:8080", "http://localhost:8080"})
LOCAL_HOSTS = frozenset({"127.0.0.1:8080", "localhost:8080"})


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    """Hash a password with salted, memory-hard scrypt using fixed parameters."""
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError("Password length is outside the supported range.")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        encoded,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64encode(salt)}${_b64encode(derived)}"


def password_hash_is_valid(encoded_hash: str | None) -> bool:
    if not encoded_hash:
        return False
    try:
        algorithm, n, r, p, salt, digest = encoded_hash.split("$", 5)
        return (
            algorithm == "scrypt"
            and int(n) == SCRYPT_N
            and int(r) == SCRYPT_R
            and int(p) == SCRYPT_P
            and len(_b64decode(salt)) == 16
            and len(_b64decode(digest)) == SCRYPT_DKLEN
        )
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded_hash: str | None) -> bool:
    if not password_hash_is_valid(encoded_hash) or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        algorithm, _, _, _, salt, expected = encoded_hash.split("$", 5)  # type: ignore[union-attr]
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError, MemoryError):
        return False


def _normalize_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = parsed.hostname.lower()
        port = parsed.port
        if port is not None and not (1 <= port <= 65535):
            return None
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        return f"{parsed.scheme}://{netloc}"
    except ValueError:
        return None


@dataclass(frozen=True)
class AuthSettings:
    password_hash: str | None
    phone_origin: str | None = None
    session_lifetime_seconds: int = SESSION_LIFETIME_SECONDS

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> "AuthSettings":
        project_root = Path(__file__).resolve().parent.parent
        load_dotenv(dotenv_path=project_root / ".env")
        source = os.environ if environ is None else environ
        phone_origin_value = source.get("DASHBOARD_PHONE_ORIGIN", "").strip()
        phone_origin = _normalize_origin(phone_origin_value) if phone_origin_value else None
        # Invalid supplied origins stay invalid instead of silently becoming public.
        if phone_origin_value and phone_origin is None:
            phone_origin = "!invalid!"
        return cls(
            password_hash=source.get("DASHBOARD_PASSWORD_HASH"),
            phone_origin=phone_origin,
        )

    @property
    def configured(self) -> bool:
        return password_hash_is_valid(self.password_hash) and (
            self.phone_origin is None or _normalize_origin(self.phone_origin) is not None
        ) and (
            isinstance(self.session_lifetime_seconds, int)
            and not isinstance(self.session_lifetime_seconds, bool)
            and 0 < self.session_lifetime_seconds <= SESSION_LIFETIME_SECONDS
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        origins = set(LOCAL_ORIGINS)
        if self.phone_origin:
            normalized = _normalize_origin(self.phone_origin)
            if normalized:
                origins.add(normalized)
        return frozenset(origins)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = set(LOCAL_HOSTS)
        if self.phone_origin:
            normalized = _normalize_origin(self.phone_origin)
            if normalized:
                hosts.add(urlsplit(normalized).netloc.lower())
        return frozenset(hosts)

    def secure_cookie_for(self, host: str, scheme: str) -> bool:
        """Use Secure on HTTPS requests and on the configured HTTPS phone host."""
        if scheme.lower() == "https":
            return True
        if not self.phone_origin:
            return False
        normalized = _normalize_origin(self.phone_origin)
        if normalized is None:
            return False
        parsed = urlsplit(normalized)
        return parsed.scheme == "https" and host.strip().lower() == parsed.netloc.lower()


@dataclass
class SessionRecord:
    key: str
    expires_at: float
    csrf_token: str
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class LoginThrottle:
    """Small process-local throttle; avoids trusting client-supplied IP headers."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: deque[float] = deque()
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def retry_after(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return max(0, int(self._locked_until - now + 0.999))

    def record_failure(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._failures.append(now)
            if len(self._failures) >= LOGIN_FAILURE_LIMIT:
                self._locked_until = max(self._locked_until, now + LOGIN_LOCK_SECONDS)
                self._failures.clear()
            return max(0, int(self._locked_until - now + 0.999))

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until = 0.0

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] > LOGIN_FAILURE_WINDOW_SECONDS:
            self._failures.popleft()
        if now >= self._locked_until:
            self._locked_until = 0.0


class SessionRegistry:
    def __init__(
        self,
        lifetime_seconds: int = SESSION_LIFETIME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lifetime_seconds = min(max(1, int(lifetime_seconds)), SESSION_LIFETIME_SECONDS)
        self._clock = clock
        self._sessions: dict[str, SessionRecord] = {}
        self._login_csrf: dict[str, float] = {}
        self._lock = threading.RLock()

    def issue_login_csrf(self) -> str:
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._login_csrf) >= LOGIN_CSRF_MAX_TOKENS:
                oldest = min(self._login_csrf, key=self._login_csrf.get)
                self._login_csrf.pop(oldest, None)
            self._login_csrf[key] = now + LOGIN_CSRF_LIFETIME_SECONDS
        return token

    def consume_login_csrf(self, token: str) -> bool:
        if not token or len(token) > 128:
            return False
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock:
            expires_at = self._login_csrf.pop(key, None)
        return expires_at is not None and expires_at > now

    def create(self) -> tuple[str, SessionRecord]:
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._clock()
        record = SessionRecord(
            key=key,
            expires_at=now + self.lifetime_seconds,
            csrf_token=secrets.token_urlsafe(32),
        )
        with self._lock:
            self._prune(now)
            self._sessions[key] = record
        return token, record

    def lookup(self, token: str | None) -> SessionRecord | None:
        if not token or len(token) > 128:
            return None
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock:
            record = self._sessions.get(key)
            if record is None:
                return None
            if record.expires_at <= now:
                self._sessions.pop(key, None)
                record.revoked.set()
                return None
            return record

    def revoke(self, record: SessionRecord) -> None:
        with self._lock:
            existing = self._sessions.pop(record.key, None)
        if existing is not None:
            existing.revoked.set()

    def is_active(self, record: SessionRecord) -> bool:
        now = self._clock()
        with self._lock:
            active = self._sessions.get(record.key) is record and record.expires_at > now
            if not active and self._sessions.get(record.key) is record:
                self._sessions.pop(record.key, None)
                record.revoked.set()
            return active

    def _prune(self, now: float) -> None:
        expired_csrf = [key for key, expiry in self._login_csrf.items() if expiry <= now]
        for key in expired_csrf:
            self._login_csrf.pop(key, None)
        expired_sessions = [key for key, record in self._sessions.items() if record.expires_at <= now]
        for key in expired_sessions:
            record = self._sessions.pop(key)
            record.revoked.set()


class AuthManager:
    def __init__(
        self,
        settings: AuthSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.sessions = SessionRegistry(settings.session_lifetime_seconds, clock=clock)
        self.throttle = LoginThrottle(clock=clock)

    def authenticate(self, password: str) -> bool:
        if not self.settings.configured:
            return False
        return verify_password(password, self.settings.password_hash)


def _request_headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    return {name.lower(): value for name, value in scope.get("headers", [])}


def _host_header(scope: dict[str, Any]) -> str:
    return _request_headers(scope).get(b"host", b"").decode("latin-1").strip().lower()


def _origin_header(scope: dict[str, Any]) -> str | None:
    value = _request_headers(scope).get(b"origin")
    if value is None:
        return None
    try:
        return _normalize_origin(value.decode("latin-1"))
    except UnicodeError:
        return None


def _session_cookie(scope: dict[str, Any]) -> str | None:
    raw_cookie = _request_headers(scope).get(b"cookie")
    if raw_cookie is None:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie.decode("latin-1"))
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel is not None else None


def _cookie_header(token: str, *, secure: bool, max_age: int) -> bytes:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE_NAME] = token
    morsel = cookie[SESSION_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["max-age"] = str(max_age)
    if secure:
        morsel["secure"] = True
    return morsel.OutputString().encode("latin-1")


def _expired_cookie_header(*, secure: bool) -> bytes:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE_NAME] = ""
    morsel = cookie[SESSION_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["max-age"] = "0"
    morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    if secure:
        morsel["secure"] = True
    return morsel.OutputString().encode("latin-1")


class DashboardAuthMiddleware:
    """Protect all HTTP and WebSocket routes, including mounted static files."""

    def __init__(self, app: Any, manager: AuthManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = _request_headers(scope)
        host = _host_header(scope)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        is_login_resource = path == "/login"

        if host not in self.manager.settings.allowed_hosts:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4403, "reason": "Invalid host."})
            else:
                await self._http_error(send, 400, "Invalid host.")
            return

        if scope_type == "websocket":
            origin = _origin_header(scope)
            if origin is None or origin not in self.manager.settings.allowed_origins:
                await send({"type": "websocket.close", "code": 4403, "reason": "Invalid origin."})
                return
        elif method not in {"GET", "HEAD", "OPTIONS"} and path != "/login":
            origin = _origin_header(scope)
            if origin is None or origin not in self.manager.settings.allowed_origins:
                await self._http_error(send, 403, "Request origin rejected.")
                return

        if not self.manager.settings.configured:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4403, "reason": "Authentication is unavailable."})
            else:
                await self._http_error(send, 503, "Dashboard authentication is not configured.")
            return

        if scope_type == "http" and is_login_resource:
            if method in {"GET", "HEAD", "POST"}:
                await self._call_with_security_headers(scope, receive, send)
            else:
                await self._http_error(send, 405, "Method not allowed.")
            return

        record = self.manager.sessions.lookup(_session_cookie(scope))
        if record is None:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "Authentication required."})
            elif path.startswith("/api/") or path.startswith("/static/") or method not in {"GET", "HEAD"}:
                await self._http_error(send, 401, "Authentication required.")
            else:
                await self._redirect_to_login(send)
            return

        if not self.manager.sessions.is_active(record):
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "Session expired."})
            else:
                await self._http_error(
                    send,
                    401,
                    "Authentication required.",
                    clear_cookie=True,
                    secure=self.manager.settings.secure_cookie_for(host, scope.get("scheme", "")),
                )
            return

        if scope_type == "http" and method not in {"GET", "HEAD", "OPTIONS"} and path != "/logout":
            csrf_header = headers.get(b"x-csrf-token", b"").decode("latin-1")
            if not csrf_header or not hmac.compare_digest(csrf_header, record.csrf_token):
                await self._http_error(send, 403, "Request verification failed.")
                return

        scope.setdefault("state", {})["auth_session"] = record
        await self._call_with_security_headers(scope, receive, send)

    async def _call_with_security_headers(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                lower_names = {name.lower() for name, _ in headers}
                additions = [
                    (b"cache-control", b"no-store, private"),
                    (b"pragma", b"no-cache"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (b"x-frame-options", b"DENY"),
                ]
                for name, value in additions:
                    if name not in lower_names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _http_error(
        send: Any,
        status: int,
        detail: str,
        *,
        clear_cookie: bool = False,
        secure: bool = False,
    ) -> None:
        body = (f'{{"detail":"{detail}"}}').encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store, private"),
            (b"pragma", b"no-cache"),
            (b"x-content-type-options", b"nosniff"),
        ]
        if clear_cookie:
            headers.append((b"set-cookie", _expired_cookie_header(secure=secure)))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _redirect_to_login(send: Any) -> None:
        await send({
            "type": "http.response.start",
            "status": 303,
            "headers": [
                (b"location", b"/login"),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store, private"),
                (b"pragma", b"no-cache"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
