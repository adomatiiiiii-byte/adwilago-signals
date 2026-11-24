from contextlib import asynccontextmanager

import pytest

from app import database as database_module
from app.main import create_app
from app.routes import market_routes
from app.security import AuthSettings, hash_password
from app.services.signal_repository import SignalRepository


TEST_PASSWORD = "isolated-auth-test-password-2026"
TEST_PHONE_ORIGIN = "https://192.168.160.174:8443"


@pytest.fixture(autouse=True)
def isolate_global_database(tmp_path, monkeypatch):
    """Keep legacy route-level repository calls inside pytest's isolated area."""
    database_path = tmp_path / "global-signals.db"
    monkeypatch.setattr(database_module, "DATABASE_PATH", database_path)
    monkeypatch.setattr(market_routes, "signal_repository", SignalRepository(database_path))
    database_module.initialize_database()


@pytest.fixture
def dashboard_test_app(tmp_path, monkeypatch):
    database_path = tmp_path / "signals.db"
    monkeypatch.setattr(database_module, "DATABASE_PATH", database_path)
    monkeypatch.setattr(market_routes.signal_repository, "database_path", database_path)
    database_module.initialize_database()

    @asynccontextmanager
    async def no_external_lifespan(_app):
        yield

    settings = AuthSettings(
        password_hash=hash_password(TEST_PASSWORD),
        phone_origin=TEST_PHONE_ORIGIN,
    )
    application = create_app(settings, lifespan_handler=no_external_lifespan)
    return application
