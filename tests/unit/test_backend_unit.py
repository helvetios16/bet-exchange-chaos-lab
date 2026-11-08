"""Pruebas unitarias del backend. No necesitan base de datos.

El TestClient se usa SIN `with`, así no se ejecuta el lifespan (que abriría el
pool de Postgres). `app.pool` queda en None, que es justo el estado "BD no
disponible" que queremos comprobar.
"""
import asyncio
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as backend


@pytest.fixture(autouse=True)
def estado_limpio(monkeypatch):
    """Cada test arranca con rate limit, circuit breaker y pool en reposo."""
    backend._hits.clear()
    monkeypatch.setattr(backend, "pool", None)
    monkeypatch.setattr(backend, "_circuit_failures", 0)
    monkeypatch.setattr(backend, "_circuit_open_until", 0.0)
    monkeypatch.setattr(backend, "RATE_LIMIT_ENABLED", True)
    yield
    backend._hits.clear()


@pytest.fixture
def client():
    return TestClient(backend.app, raise_server_exceptions=False)


def _request(ip="10.0.0.1"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=ip))


# --------------------------------------------------------------------------
# round2: redondeo monetario half-up (no el banker's rounding de Python)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "entrada, esperado",
    [(0.125, 0.13), (2.675, 2.68), (1.005, 1.01), (10, 10.0), (0.0, 0.0), (1.994, 1.99)],
)
def test_round2_half_up(entrada, esperado):
    assert backend.round2(entrada) == esperado


def test_round2_difiere_del_round_de_python():
    # Es la razón de existir de round2: round(0.125, 2) da 0.12
    assert round(0.125, 2) == 0.12
    assert backend.round2(0.125) == 0.13


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
def test_cfg_int_usa_default_si_el_valor_no_es_numerico(monkeypatch):
    monkeypatch.setenv("X_TEST_INT", "no-es-un-numero")
    assert backend._cfg_int("X_TEST_INT", 7) == 7


def test_cfg_int_lee_el_entorno(monkeypatch):
    monkeypatch.setenv("X_TEST_INT", "42")
    assert backend._cfg_int("X_TEST_INT", 7) == 42


def test_dsn_incluye_los_parametros_de_conexion(monkeypatch):
    monkeypatch.setattr(backend, "DB_HOST", "db.test")
    monkeypatch.setattr(backend, "DB_PORT", 6543)
    dsn = backend._dsn()
    assert "host=db.test" in dsn
    assert "port=6543" in dsn
    assert "application_name=kuber-backend" in dsn


# --------------------------------------------------------------------------
# Rate limit
# --------------------------------------------------------------------------
def test_rate_limit_bloquea_al_superar_el_maximo(monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_REQUESTS", 3)
    for _ in range(3):
        backend.rate_limit(_request())
    with pytest.raises(HTTPException) as exc:
        backend.rate_limit(_request())
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_rate_limit_es_por_ip(monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_REQUESTS", 1)
    backend.rate_limit(_request("10.0.0.1"))
    backend.rate_limit(_request("10.0.0.2"))  # otra IP: no debe fallar


def test_rate_limit_desactivado_no_bloquea(monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(backend, "RATE_LIMIT_REQUESTS", 1)
    for _ in range(10):
        backend.rate_limit(_request())


def test_rate_limit_la_ventana_expira(monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(backend, "RATE_LIMIT_WINDOW_SECONDS", 60)
    # Una petición "vieja" (fuera de la ventana) no cuenta
    backend._hits["10.0.0.1"] = [backend.time.time() - 120]
    backend.rate_limit(_request("10.0.0.1"))


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------
def test_circuit_se_abre_al_alcanzar_el_umbral(monkeypatch):
    monkeypatch.setattr(backend, "CIRCUIT_FAIL_THRESHOLD", 3)
    for _ in range(2):
        backend.record_db_failure()
    assert backend.circuit_state() is False
    backend.record_db_failure()
    assert backend.circuit_state() is True


def test_circuit_se_cierra_tras_un_exito(monkeypatch):
    monkeypatch.setattr(backend, "CIRCUIT_FAIL_THRESHOLD", 1)
    backend.record_db_failure()
    assert backend.circuit_state() is True
    backend.record_db_success()
    assert backend.circuit_state() is False


def test_circuit_pasa_a_half_open_cuando_expira(monkeypatch):
    monkeypatch.setattr(backend, "CIRCUIT_FAIL_THRESHOLD", 1)
    monkeypatch.setattr(backend, "CIRCUIT_RESET_SECONDS", 0)
    backend.record_db_failure()
    # Con reset de 0 s el circuito ya venció: se permite un intento de prueba
    assert backend.circuit_state() is False


def test_fetch_responde_503_con_el_circuito_abierto(monkeypatch):
    monkeypatch.setattr(backend, "pool", object())  # != None
    monkeypatch.setattr(backend, "CIRCUIT_FAIL_THRESHOLD", 1)
    backend.record_db_failure()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(backend.fetch("SELECT 1"))
    assert exc.value.status_code == 503
    assert "Retry-After" in exc.value.headers


# --------------------------------------------------------------------------
# Sondas de salud
# --------------------------------------------------------------------------
def test_startup_responde_ok(client):
    r = client.get("/health/startup")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_liveness_no_depende_de_la_base_de_datos(client):
    """Con el pool sin inicializar la liveness debe seguir respondiendo 200:
    si dependiera de la BD, una caída de Postgres reiniciaría todos los pods."""
    assert backend.pool is None
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_falla_sin_pool(client):
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not-ready"


# --------------------------------------------------------------------------
# Validación de POST /bets (se valida antes de tocar la BD)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {},  # faltan user_id y odds_id
        {"user_id": 1},  # falta odds_id
        {"user_id": 1, "odds_id": 1, "stake": 0},  # stake cero
        {"user_id": 1, "odds_id": 1, "stake": -5},  # stake negativo
        {"user_id": 1, "odds_id": 1, "stake": 10_001},  # sobre el máximo
        {"user_id": 1, "odds_id": 1, "stake": 10, "side": "DRAW"},  # side inválido
    ],
)
def test_place_bet_rechaza_peticiones_invalidas(client, payload):
    r = client.post("/api/v1/bets", json=payload)
    assert r.status_code == 400
    assert "error" in r.json()


def test_place_bet_valido_sin_pool_da_503(client):
    r = client.post("/api/v1/bets", json={"user_id": 1, "odds_id": 1, "stake": 10})
    assert r.status_code == 503


def test_settle_sin_pool_da_503(client):
    r = client.post("/api/v1/bets/1/settle", json={"won": True})
    assert r.status_code == 503


# --------------------------------------------------------------------------
# Endpoints varios y métricas
# --------------------------------------------------------------------------
def test_whoami_devuelve_el_pod(client):
    r = client.get("/api/v1/whoami")
    assert r.status_code == 200
    assert r.json()["pod"] == backend.POD_NAME


def test_version_devuelve_version_y_commit(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == backend.app.version
    assert body["commit"] == "abc1234"


def test_endpoint_con_bd_caida_devuelve_error_json_no_traza(client):
    r = client.get("/api/v1/events")
    assert r.status_code == 500
    assert r.json()["error"] == "internal error"


def test_respuestas_llevan_la_cabecera_x_pod_name(client):
    r = client.get("/api/v1/whoami")
    assert r.headers["X-Pod-Name"] == backend.POD_NAME


def test_metrics_expone_formato_prometheus(client):
    client.get("/api/v1/whoami")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text
    assert "http_request_duration_seconds" in r.text


def test_metricas_agrupan_rutas_con_id_para_evitar_cardinalidad(client, monkeypatch):
    async def fetch_vacio(query, params=()):
        return []  # el endpoint responde 404 de forma controlada

    monkeypatch.setattr(backend, "fetch", fetch_vacio)
    for market_id in (11, 22, 33):
        client.get(f"/api/v1/markets/{market_id}")
    texto = client.get("/metrics").text
    assert 'path="/api/v1/markets/:id"' in texto
    assert 'path="/api/v1/markets/11"' not in texto


def test_rate_limit_via_http_devuelve_429(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_REQUESTS", 2)
    codes = [client.get("/api/v1/events").status_code for _ in range(3)]
    assert codes[-1] == 429
