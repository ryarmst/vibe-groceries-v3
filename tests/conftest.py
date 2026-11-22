import json
import os
import tempfile
from pathlib import Path

# Settings are read at import time; point them at a throwaway data dir before importing the app.
_TMP = tempfile.mkdtemp(prefix="shopping-tests-")
os.environ.update({"DATA_DIR": _TMP, "PCX_API_KEY": "test-key", "SHOPPING_ENV_FILE": "/nonexistent", "STORE_ID": "1234", "DEAL_SCAN_HOURS": "0", "JOBS_ENABLED": "0"})

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from shopping.models import Base  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


@pytest.fixture(scope="session", autouse=True)
def migrated_app_db():
    """Tests that go through session_scope() use the app database in the temp DATA_DIR."""
    from shopping.database import run_migrations

    run_migrations()
