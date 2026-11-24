"""Dashboard market-data API routes."""

import asyncio
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

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
            "health": {
                "overall_state": "unavailable",
                "rest_status": "disconnected",
                "websocket_status": "disconnected",
                "websocket_activity": "disconnected",
                "last_successful_rest_request": None,
                "rest_age_seconds": None,
                "last_valid_tick_timestamp": None,
                "last_valid_tick_received_at": None,
                "tick_age_seconds": None,
                "latest_completed_candle_timestamp": None,
                "latest_completed_candle_open_time": None,
                "latest_completed_candle_close_time": None,
                "latest_completed_candle_age_seconds": None,
                "latest_reconciliation_new_completed_candle": None,
                "last_new_completed_candle_at": None,
                "candle_sequence_state": "unknown",
                "session_state": "unknown",
                "trade_mode": None,
                "session_information_available": False,
                "gap_detected": False,
                "missing_intervals": [],
                "rest_freshness": "stale",
                "candle_freshness": "stale",
                "rest_error": "Exness market-data service is not configured.",
                "websocket_error": None,
                "session_error": "Exness market-data service is not configured.",
            },
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


@router.get("/api/signals/{signal_id}/evidence")
async def signal_evidence(signal_id: int) -> dict:
    """Read saved audit evidence for one signal without recalculating it."""
    signal = signal_repository.get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found.")
    evidence = signal_repository.get_evidence(signal_id)
    if evidence is not None:
        return {"signal_id": signal_id, "available": True, "signal": signal, "evidence": evidence}
    return {
        "signal_id": signal_id,
        "available": False,
        "message": "Historical evidence unavailable",
    }


@router.websocket("/ws/market-data")
async def market_data_stream(websocket: WebSocket) -> None:
    session = websocket.scope.get("state", {}).get("auth_session")
    if session is None:
        await websocket.close(code=4401, reason="Authentication required.")
        return
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
            queue_task = asyncio.create_task(queue.get())
            revoked_task = asyncio.create_task(session.revoked.wait())
            disconnect_task = asyncio.create_task(websocket.receive())
            remaining = max(0.0, session.expires_at - time.monotonic())
            done, pending = await asyncio.wait(
                {queue_task, revoked_task, disconnect_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                await websocket.close(code=4401, reason="Session expired.")
                break
            if revoked_task in done:
                await websocket.close(code=4401, reason="Session revoked.")
                break
            if disconnect_task in done:
                if disconnect_task.result().get("type") == "websocket.disconnect":
                    break
                continue
            if time.monotonic() >= session.expires_at:
                await websocket.close(code=4401, reason="Session expired.")
                break
            await websocket.send_json(queue_task.result())
    except WebSocketDisconnect:
        pass
    finally:
        service.unsubscribe_dashboard(queue)
