"""FastAPI entry point for the dashboard."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import initialize_database
from app.routes.market_routes import configure_market_service, router as market_router
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


app = FastAPI(
    title="ADWILAGO Trading Signals",
    description="A manual-review dashboard for USDJPY trading signals.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")
app.include_router(market_router)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the dashboard with deliberately disconnected market data."""
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "market": "USDJPY",
            "timeframe": "5 minutes",
            "strategy": "DayTrading Engulfing",
        },
    )
