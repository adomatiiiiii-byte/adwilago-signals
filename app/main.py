"""FastAPI entry point for the dashboard."""

from contextlib import asynccontextmanager
import hmac
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import initialize_database
from app.routes.market_routes import configure_market_service, router as market_router
from app.security import (
    AuthManager,
    AuthSettings,
    DashboardAuthMiddleware,
    SESSION_COOKIE_NAME,
    _cookie_header,
    _normalize_origin,
)
from app.services.exness_service import ExnessConfig, ExnessMarketDataService, ExnessError
from app.services.signal_repository import SignalRepository
from app.services.strategy_engine import DayTradingEngulfingStrategy


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Prepare local application storage and the read-only market service."""
    initialize_database()
    try:
        market_service = ExnessMarketDataService(
            ExnessConfig.from_env(),
            strategy_engine=DayTradingEngulfingStrategy(),
            signal_repository=SignalRepository(),
        )
        configure_market_service(market_service)
        await market_service.start()
    except ExnessError:
        configure_market_service(None)
    yield
    from app.routes import market_routes
    if market_routes.service is not None:
        await market_routes.service.close()


def create_app(
    auth_settings: AuthSettings | None = None,
    *,
    lifespan_handler=lifespan,
) -> FastAPI:
    """Build the dashboard app with an isolated, in-memory auth registry."""
    application = FastAPI(
        title="ADWILAGO Trading Signals",
        description="A manual-review dashboard for USDJPY trading signals.",
        lifespan=lifespan_handler,
    )
    manager = AuthManager(auth_settings or AuthSettings.from_environment())
    application.state.auth_manager = manager
    templates = Jinja2Templates(directory="app/templates")
    application.add_middleware(DashboardAuthMiddleware, manager=manager)
    application.mount("/static", StaticFiles(directory="static"), name="static")
    application.include_router(market_router)

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        csrf_token = manager.sessions.issue_login_csrf()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": csrf_token, "error": None},
            headers={"Cache-Control": "no-store, private"},
        )

    @application.post("/login")
    async def login(request: Request):
        origin = _normalize_origin(request.headers.get("origin", ""))
        if origin is None or origin not in manager.settings.allowed_origins:
            return HTMLResponse("Request origin rejected.", status_code=403)

        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = await request.body()
        if content_type != "application/x-www-form-urlencoded" or len(body) > 8192:
            return HTMLResponse("Invalid login request.", status_code=400)
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return HTMLResponse("Invalid login request.", status_code=400)
        csrf_token = form.get("csrf_token", [""])[0]
        password = form.get("password", [""])[0]
        if not manager.sessions.consume_login_csrf(csrf_token):
            return HTMLResponse("Login request expired. Reload the page and try again.", status_code=400)

        retry_after = manager.throttle.retry_after()
        if retry_after:
            return HTMLResponse(
                "Login is temporarily unavailable. Try again later.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not manager.authenticate(password):
            retry_after = manager.throttle.record_failure()
            next_csrf = manager.sessions.issue_login_csrf()
            response = templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf_token": next_csrf, "error": "Invalid username or password."},
                status_code=429 if retry_after else 401,
                headers={"Retry-After": str(retry_after)} if retry_after else None,
            )
            return response

        manager.throttle.record_success()
        session_id, _ = manager.sessions.create()
        response = RedirectResponse("/", status_code=303)
        response.raw_headers.append((
            b"set-cookie",
            _cookie_header(
                session_id,
                secure=manager.settings.secure_cookie_for(
                    request.headers.get("host", ""), request.scope.get("scheme", "")
                ),
                max_age=manager.sessions.lifetime_seconds,
            ),
        ))
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @application.post("/logout")
    async def logout(request: Request):
        session = request.state.auth_session
        origin = _normalize_origin(request.headers.get("origin", ""))
        if origin is None or origin not in manager.settings.allowed_origins:
            return HTMLResponse("Request origin rejected.", status_code=403)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = await request.body()
        if content_type != "application/x-www-form-urlencoded" or len(body) > 8192:
            return HTMLResponse("Invalid logout request.", status_code=400)
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return HTMLResponse("Invalid logout request.", status_code=400)
        supplied_csrf = form.get("csrf_token", [""])[0]
        if not supplied_csrf or not hmac.compare_digest(supplied_csrf, session.csrf_token):
            return HTMLResponse("Request verification failed.", status_code=403)

        manager.sessions.revoke(session)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=manager.settings.secure_cookie_for(
                request.headers.get("host", ""), request.scope.get("scheme", "")
            ),
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @application.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Render the dashboard for the authenticated user."""
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "market": "USDJPY",
                "timeframe": "5 minutes",
                "strategy": "DayTrading Engulfing",
                "logout_csrf_token": request.state.auth_session.csrf_token,
            },
        )

    return application


app = create_app()
