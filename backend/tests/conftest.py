import os

# Point everything at the test database before any app module reads settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/checkout_guard_test"
)
os.environ["SIMULATOR_ENABLED"] = "true"

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from app.config import SANDBOX_API_BASE, Settings, get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.bootstrap import ensure_shop  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.guard.check import invalidate_cache  # noqa: E402
from app.paypal.client import PayPalClient  # noqa: E402
from tests.sandbox_double import SandboxDouble  # noqa: E402

get_settings.cache_clear()
SHOP = get_settings().shop_id


@pytest.fixture
async def db():
    """Fresh schema per test on the real Postgres test database."""
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    engine = db_session.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    invalidate_cache()
    async with db_session.get_sessionmaker()() as s:
        await ensure_shop(s, SHOP)
        yield s
    await engine.dispose()


@pytest.fixture
async def sandbox():
    """Stateful PayPal sandbox double (tests/sandbox_double.py) behind respx, plus a real PayPalClient
    pointed at it as `sandbox.client`. Any unmocked PayPal URL fails the test instead of going out."""
    double = SandboxDouble()
    settings = Settings(
        _env_file=None,
        paypal_client_id="test-id",
        paypal_client_secret="test-secret",
        paypal_webhook_id="WH-1",
        public_app_url="https://shop.example.test",
    )
    with respx.mock(base_url=SANDBOX_API_BASE, assert_all_called=False) as router:
        double.install(router)
        double.client = PayPalClient(
            settings, http=httpx.AsyncClient(base_url=SANDBOX_API_BASE), max_retries=0
        )
        yield double
        await double.client.aclose()
