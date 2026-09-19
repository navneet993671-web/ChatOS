"""Shared test configuration — ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db


# ---------------------------------------------------------------------------
# Genuine core.database capture — fixes an order-dependence bug in this suite.
#
# Several test modules replace sys.modules entries for core modules with
# MagicMock stubs at IMPORT time and never restore them (see
# tests/test_auth_event_loop.py:68 and tests/test_auth_regressions.py:69).
# pytest collects test modules alphabetically, so anything imported after those
# modules receives a mock instead of the real ORM models — tests then pass in
# isolation and fail in the full suite (or raise a SQLAlchemy "metaclass
# conflict" when the real module is re-loaded alongside a stubbed one).
#
# conftest is imported before any test module, so capturing the real module
# here gives tests that genuinely need the database a reference that test
# order cannot poison.
# ---------------------------------------------------------------------------
import pytest as _pytest  # noqa: E402

try:
    import core.database as _genuine_core_database  # noqa: E402,F401
except Exception:  # pragma: no cover - only when the DB layer cannot import
    _genuine_core_database = None


@_pytest.fixture
def real_core_database():
    """The genuine ``core.database`` module, immune to test-order stubbing."""
    if _genuine_core_database is None:
        _pytest.skip("core.database could not be imported in this environment")
    return _genuine_core_database
