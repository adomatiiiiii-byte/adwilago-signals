"""Dashboard market-data API routes."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.exness_service import ExnessError, ExnessMarketDataService
from app.services.signal_repository import SignalRepository


router = APIRouter()
service: ExnessMarketDataService | None = None
signal_repository = SignalRepository()


def configure_market_service(value: ExnessMarketDataService | None) -> None:
    global service
    service = value


def _signal_snapshot() -> dict:
    if service is not None:
        return service.get_signal_snapshot()
    return {
        "current_signal": None,
        "signal_history": signal_repository.list_recent(),
        "today_signals": signal_repository.list_today_session(),
    }


@router.get("/api/market-data")
async def market_data() -> dict:
    if service is None:
        return {
            "status": "disconnected",
            "market": "USDJPY",
            "timeframe": "M5",
            "provider": "Exness",
            "candles": [],
            "chart_candles": [],
            "error": "Exness market-data service is not configured.",
            **_signal_snapshot(),
        }
    try:
        snapshot = await service.get_snapshot(refresh=True)
        service.start_tick_stream()
        return snapshot
    except ExnessError as exc:
        return {
            "status": "error",
            "market": "USDJPY",
            "timeframe": "M5",
            "provider": "Exness",
            "candles": [],
            "chart_candles": [],
            "error": str(exc),
            **_signal_snapshot(),
        }


@router.get("/api/signals")
async def signals() -> dict:
    """Read current signal state and persisted history without market refresh."""
    return _signal_snapshot()


@router.websocket("/ws/market-data")
async def market_data_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    if service is None:
        await websocket.send_json({"type": "status", "status": "disconnected", "error": "Exness market-data service is not configured."})
        await websocket.close()
        return

    queue = service.subscribe_dashboard()
    try:
        await websocket.send_json({"type": "snapshot", **await service.get_snapshot()})
        service.start_tick_stream()
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        service.unsubscribe_dashboard(queue)
