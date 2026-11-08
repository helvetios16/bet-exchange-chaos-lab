"""Fixtures de integración: API real + PostgreSQL real.

Variables (con defaults pensados para un Postgres local o el service container
del CI):  TEST_DB_HOST, TEST_DB_PORT, TEST_DB_NAME, TEST_DB_USER,
TEST_DB_PASSWORD.

Si no hay Postgres, los tests se SALTAN en local. En CI se define
REQUIRE_DB=1 y entonces FALLAN: así una base caída no puede dejar el
pipeline en verde sin haber probado nada.
"""
import os
import pathlib
import subprocess
import sys

import psycopg
import pytest
from fastapi.testclient import TestClient

import app as backend

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "app" / "backend"

DB = {
    "host": os.getenv("TEST_DB_HOST", "localhost"),
    "port": int(os.getenv("TEST_DB_PORT", "5432")),
    "dbname": os.getenv("TEST_DB_NAME", "kuber_test"),
    "user": os.getenv("TEST_DB_USER", "kuber"),
    "password": os.getenv("TEST_DB_PASSWORD", "kuber"),
}


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def seed_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("seeds")
    subprocess.run(
        [sys.executable, str(BACKEND / "seed.py"), "--out", str(out),
         "--events", "6", "--hot", "3", "--users", "30"],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture(scope="session")
def api(seed_dir):
    """TestClient con lifespan: aplica schema.sql y carga las semillas."""
    try:
        with psycopg.connect(connect_timeout=3, **DB):
            pass
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_DB") == "1":
            pytest.fail(f"PostgreSQL no disponible y REQUIRE_DB=1: {exc}")
        pytest.skip(f"PostgreSQL no disponible: {exc}")

    mp = pytest.MonkeyPatch()
    mp.setattr(backend, "DB_HOST", DB["host"])
    mp.setattr(backend, "DB_PORT", DB["port"])
    mp.setattr(backend, "DB_NAME", DB["dbname"])
    mp.setattr(backend, "DB_USER", DB["user"])
    mp.setattr(backend, "DB_PASSWORD", DB["password"])
    mp.setattr(backend, "SEED_DIR", str(seed_dir))
    mp.setattr(backend, "SEED_ON_START", True)
    mp.setattr(backend, "RATE_LIMIT_ENABLED", False)
    mp.setenv("SCHEMA_PATH", str(BACKEND / "schema.sql"))
    with TestClient(backend.app) as client:
        yield client
    mp.undo()


@pytest.fixture(scope="session")
def mercado_en_juego(api):
    """(market_id, [odds_id,...]) de un mercado abierto de un evento IN_PLAY."""
    events = api.get("/api/v1/events", params={"status": "IN_PLAY"}).json()["events"]
    assert events, "las semillas deberían traer eventos IN_PLAY"
    markets = api.get(f"/api/v1/events/{events[0]['id']}/markets").json()["markets"]
    market_id = markets[0]["id"]
    sels = api.get(f"/api/v1/markets/{market_id}").json()["selections"]
    odds_ids = [s["odds_id"] for s in sels if s["odds_id"]]
    assert odds_ids
    return market_id, odds_ids


@pytest.fixture
def usuario(api):
    """Un usuario distinto por test, para que los saldos no se pisen."""
    counter = getattr(usuario, "_n", 0) + 1
    usuario._n = counter
    return counter  # ids 1..30 existen en las semillas
