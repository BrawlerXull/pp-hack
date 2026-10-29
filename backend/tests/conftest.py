import ipaddress
import os
import socket
from urllib.parse import urlsplit

# Before any app module reads settings: never load the repo .env (it holds real Gemini/PayPal credentials),
# blank every credential an exported shell variable could still supply, and use the test database.
os.environ["CHECKOUT_GUARD_NO_ENV_FILE"] = "1"
for _var in (
    "GEMINI_API_KEY",
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_WEBHOOK_ID",
    "NOTIFY_WEBHOOK_URL",
):
    os.environ[_var] = ""
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/checkout_guard_test"
)
os.environ["SIMULATOR_ENABLED"] = "true"

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from app.ai import breaker, gemini  # noqa: E402
from app.api import ratelimit  # noqa: E402
from app.config import SANDBOX_API_BASE, Settings, get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.bootstrap import ensure_shop  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.guard import devices  # noqa: E402
from app.guard.check import invalidate_cache  # noqa: E402
from app.paypal.client import PayPalClient  # noqa: E402
from tests.sandbox_double import SandboxDouble  # noqa: E402

get_settings.cache_clear()
SHOP = get_settings().shop_id


async def blocked(session) -> list[str]:
    """Device fingerprints blocked right now (app/guard/devices.py), sorted."""
    return sorted(await devices.blocked(session, SHOP))


def _db_addresses() -> set[str]:
    host = urlsplit(os.environ["DATABASE_URL"]).hostname or "localhost"
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


_DB_ADDRESSES = _db_addresses()


def _local(address: object) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path
        return True
    host = str(address[0])
    if host in _DB_ADDRESSES or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that reaches the internet or the real Gemini client. Mocked HTTP (respx) never opens a
    socket, so it is unaffected; the Postgres test database is local. Attempts are recorded and failed at
    teardown too, because app code that catches errors (AI fallback, PayPal retries) would hide them."""
    leaks: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(real):
        def connect(self, address):
            if not _local(address):
                leaks.append(f"outbound connection to {address!r}")
                raise OSError(f"tests must not use the network (tried {address!r}); mock it with respx")
            return real(self, address)

        return connect

    def no_gemini():
        leaks.append("real Gemini client requested")
        raise RuntimeError("unmocked Gemini call: patch app.ai.gemini._client or generate_json")

    monkeypatch.setattr(socket.socket, "connect", guard(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(real_connect_ex))
    monkeypatch.setattr(gemini, "_client", no_gemini)
    # Breaker state is process-global: start each test clean, in memory unless a test opts into the DB store.
    ratelimit.reset()  # per-process request counts (app/api/ratelimit.py) start empty in every test
    monkeypatch.setattr(gemini, "_breaker", breaker.Breaker())
    monkeypatch.setattr(gemini, "_backup_breaker", breaker.Breaker())
    monkeypatch.setattr(gemini, "_backup_client", lambda key: no_gemini())
    monkeypatch.setattr(gemini, "store", breaker.MemoryStore())
    yield leaks
    if leaks:
        pytest.fail("; ".join(leaks))


@pytest.fixture
async def db():
    """Fresh schema per test on the real Postgres test database. It drops and recreates the schema, so two
    pytest runs must not share one database at the same time: give a parallel run its own via
    TEST_DATABASE_URL (e.g. .../checkout_guard_test_2)."""
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
