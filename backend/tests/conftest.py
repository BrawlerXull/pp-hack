import os

# Point everything at the test database before any app module reads settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/checkout_guard_test"
)
os.environ["SIMULATOR_ENABLED"] = "true"

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.bootstrap import ensure_shop  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.guard.check import invalidate_cache  # noqa: E402

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
