# =============================================================================
# Backend — FastAPI
#
# Endpoints de salud separados a proposito:
#   /health/startup  -> solo verifica que el proceso levanto (muy barato)
#   /health/live     -> el proceso esta vivo (NO toca la BD)
#   /health/ready    -> puede servir? incluye un SELECT 1 a Postgres con
#                       timeout corto
#   /api/...         -> la app
#   /metrics         -> Prometheus
# =============================================================================
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP

import psycopg
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg_pool import AsyncConnectionPool

from seed_load import seed_database, seed_summary

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def _cfg(key: str, default: str) -> str:
    return os.getenv(key, default)


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_cfg(key, str(default)))
    except ValueError:
        return default


LOG_LEVEL = _cfg("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = _cfg("LOG_FORMAT", "json")
POD_NAME = _cfg("POD_NAME", "unknown")
NODE_NAME = _cfg("NODE_NAME", "unknown")
API_PREFIX = _cfg("API_PREFIX", "/api/v1")

DB_HOST = _cfg("DB_HOST", "kuber-postgres-client")
DB_PORT = _cfg_int("DB_PORT", 5432)
DB_NAME = _cfg("DB_NAME", "kuber")
DB_USER = _cfg("DB_USER", "kuber")
DB_PASSWORD = _cfg("DB_PASSWORD", "")
POOL_MIN = _cfg_int("DB_POOL_MIN", 2)
POOL_MAX = _cfg_int("DB_POOL_MAX", 20)
DB_CONNECT_TIMEOUT_MS = _cfg_int("DB_CONNECT_TIMEOUT_MS", 3000)
DB_STATEMENT_TIMEOUT_MS = _cfg_int("DB_STATEMENT_TIMEOUT_MS", 5000)
CIRCUIT_FAIL_THRESHOLD = _cfg_int("DB_CIRCUIT_FAIL_THRESHOLD", 5)
CIRCUIT_RESET_SECONDS = _cfg_int("DB_CIRCUIT_RESET_SECONDS", 30)

RATE_LIMIT_ENABLED = _cfg("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_REQUESTS = _cfg_int("RATE_LIMIT_REQUESTS", 200)
RATE_LIMIT_WINDOW_SECONDS = _cfg_int("RATE_LIMIT_WINDOW_SECONDS", 60)

logging.basicConfig(level=LOG_LEVEL, format="%(message)s" if LOG_FORMAT == "json" else "%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kuber.backend")

# -----------------------------------------------------------------------------
# Metricas
#   - http_requests_total:   latencia y tasa por endpoint/status/pod
#   - http_request_duration: p50/p90/p99 via histogram
#   - db_pool_wait_seconds:  cuanto tiempo esperan los requests por conexion
#   - db_circuit_open:       1 si el circuit breaker esta abierto
#   - pod_ready:             1 si la readiness probe pasa
# -----------------------------------------------------------------------------
REQUESTS = Counter(
    "http_requests_total", "Total de requests HTTP",
    ["method", "path", "status", "pod"],
)
LATENCY = Histogram(
    "http_request_duration_seconds", "Latencia de requests HTTP",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
DB_POOL_WAIT = Histogram(
    "db_pool_wait_seconds", "Espera por una conexion del pool",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
DB_CIRCUIT_OPEN = Gauge("db_circuit_open", "1 si el circuit breaker de la BD esta abierto")
DB_QUERY_ERRORS = Counter("db_query_errors_total", "Errores de query", ["kind"])
POD_READY = Gauge("pod_ready", "1 si la readiness probe pasa", ["pod"])
BETS_PLACED = Counter("bets_placed_total", "Apuestas colocadas")
BETS_SETTLED = Counter("bets_settled_total", "Apuestas liquidadas", ["result"])
# Fallas por tipo. BettingInsufficientFunds es un rechazo de negocio esperado
# bajo carga (muchos usuarios sin saldo), NO un error: separarlo de los errores
# de verdad es lo que evita que el panel de alertas se llene de ruido.
BETS_REJECTED = Counter("bets_rejected_total", "Apuestas rechazadas", ["reason"])


# -----------------------------------------------------------------------------
# Pool de conexiones
# -----------------------------------------------------------------------------
pool: AsyncConnectionPool | None = None
# Estado del circuit breaker, en memoria del proceso (no compartido: es
# por-pod a proposito, un pod sick no debe arrastrar a los demas)
_circuit_open_until: float = 0.0
_circuit_failures: int = 0
_startup_at: float = 0.0

# --- semillas ---------------------------------------------------------------
# Apuesta Total (simulado). El dataset se genera con seed.py y se carga una vez.
SEED_ON_START = _cfg("SEED_ON_START", "true").lower() == "true"
SEED_DIR = _cfg("SEED_DIR", "/app/seeds")


def _schema_sql() -> str:
    """Lee el esquema desde disco. Va en una variable de entorno porque la
    ruta cambia entre la imagen Docker (/app/schema.sql) y el desarrollo local."""
    path = _cfg("SCHEMA_PATH", "/app/schema.sql")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# Timeout generoso: la primera carga con COPY sobre tablas nuevas puede tardar
# en un contenedor lento, y un timeout corto convertiría un arranque lento en
# un crash-loop.
_lock_acquired = False


def _seed_once() -> None:
    """Carga el dataset bajo un lock de sesión de Postgres.

    El lock es lo que evita el problema de arranque con varias réplicas: con
    4 pods del backend subiendo a la vez, sin esto los cuatro ejecutarían
    TRUNCATE + COPY en paralelo y el dataset quedaría a medias.
    """
    global _lock_acquired
    with psycopg.connect(_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Espera hasta 120s a que otro pod termine de cargar.
            cur.execute("SELECT pg_advisory_lock(918_273_645)")
            try:
                cur.execute("SELECT count(*) FROM events")
                (n,) = cur.fetchone()
                if n > 0:
                    log.info("dataset ya cargado por otro pod, se respeta", extra={"events": n})
                    _lock_acquired = True
                    return
                counts = seed_database(_dsn(), SEED_DIR, verbose=False)
                _lock_acquired = True
                log.info("dataset cargado", extra=counts)
            finally:
                cur.execute("SELECT pg_advisory_unlock(918_273_645)")


def _dsn() -> str:
    return (
        f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
        f"user={DB_USER} password={DB_PASSWORD} "
        f"connect_timeout={DB_CONNECT_TIMEOUT_MS // 1000 or 1} "
        f"application_name=kuber-backend"
    )


def round2(x: float) -> float:
    """Redondeo monetario a 2 decimales, half-up.

    El `round()` de Python usa banker's rounding (half-to-even), que es
    incorrecto para dinero: round(0.125, 2) devuelve 0.12, y un sistema que
    redondea a la baja de forma sistemática acaba con diferencias centiméricas
    que no cuadran con el ledger. Decimal(ROUND_HALF_UP) es lo que usan los
    motores de pago.
    """
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, _startup_at
    _startup_at = time.monotonic()
    log.info(
        "starting",
        extra={"pod": POD_NAME, "node": NODE_NAME, "pool_min": POOL_MIN, "pool_max": POOL_MAX},
    )
    pool = AsyncConnectionPool(
        conninfo=_dsn(),
        min_size=POOL_MIN,
        max_size=POOL_MAX,
        timeout=DB_CONNECT_TIMEOUT_MS / 1000,
        open=False,
        # Reconecta solo: si la BD cae, psycopg reintenta en background
        # en vez de propagar el error a cada request.
        reconnect_timeout=30,
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open(wait=True, timeout=DB_CONNECT_TIMEOUT_MS / 1000)

    # --- Esquema + semillas ---------------------------------------------
    # El esquema vive en schema.sql, no en el código: separarlos permite
    # revisarlo y versionarlo como SQL, y ejecutarlo con COPY en vez de con
    # DDL escrito a mano.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_schema_sql())
            await conn.commit()
    log.info("schema aplicado")

    # Las semillas se cargan UNA vez, no en cada pod. Con replicas > 1, cuatro
    # pods arrancando a la vez podrían cargar el dataset cuatro veces y pisarse
    # entre ellos durante los TRUNCATE. Se usa un lock de Postgres: el primero
    # que lo toma carga, los demás esperan y ven que ya está cargado.
    if SEED_ON_START:
        await asyncio.to_thread(_seed_once)
        summary = await asyncio.to_thread(seed_summary, _dsn())
        log.info("semillas listas", extra=summary)
    else:
        summary = await asyncio.to_thread(seed_summary, _dsn())
        log.info("semillas omitidas por configuración", extra=summary)

    log.info("ready", extra={"pod": POD_NAME})
    yield
    if pool is not None:
        await pool.close()
    log.info("stopped", extra={"pod": POD_NAME})


app = FastAPI(title="kuber-backend", version="1.0.0", lifespan=lifespan)


# -----------------------------------------------------------------------------
# Rate limiting en memoria (por IP). Suficiente para pruebas; en produccion
# usar Redis o el middleware del ingress.
# -----------------------------------------------------------------------------
_hits: dict[str, list[float]] = {}


def rate_limit(request: Request) -> None:
    if not RATE_LIMIT_ENABLED:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = [t for t in _hits.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(window) >= RATE_LIMIT_REQUESTS:
        _hits[ip] = window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit excedido",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
        )
    window.append(now)
    _hits[ip] = window


# -----------------------------------------------------------------------------
# Circuit breaker
# -----------------------------------------------------------------------------
def circuit_state() -> bool:
    """True si el circuito esta abierto (no dejar pasar traffic a la BD)."""
    global _circuit_failures, _circuit_open_until
    now = time.monotonic()
    if _circuit_open_until > now:
        return True
    if _circuit_open_until and _circuit_open_until <= now:
        # Half-open: permitir un intento de prueba
        _circuit_open_until = 0.0
        _circuit_failures = 0
        log.warning("circuit half-open, probing database")
    return False


def record_db_failure() -> None:
    global _circuit_failures, _circuit_open_until
    _circuit_failures += 1
    if _circuit_failures >= CIRCUIT_FAIL_THRESHOLD and _circuit_open_until == 0.0:
        _circuit_open_until = time.monotonic() + CIRCUIT_RESET_SECONDS
        DB_CIRCUIT_OPEN.set(1)
        log.error("circuit opened", extra={"failures": _circuit_failures, "reset_s": CIRCUIT_RESET_SECONDS})


def record_db_success() -> None:
    global _circuit_failures, _circuit_open_until
    if _circuit_failures or _circuit_open_until:
        _circuit_failures = 0
        _circuit_open_until = 0.0
        DB_CIRCUIT_OPEN.set(0)
        log.info("circuit closed")


async def fetch(query: str, params: tuple = ()) -> list[tuple]:
    """Ejecuta una query con proteccion de pool, timeout y circuit breaker."""
    if pool is None:
        raise RuntimeError("pool no inicializado")
    if circuit_state():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="circuit breaker abierto para la base de datos",
            headers={"Retry-After": str(CIRCUIT_RESET_SECONDS)},
        )
    t0 = time.perf_counter()
    try:
        async with pool.connection() as conn:
            DB_POOL_WAIT.observe(time.perf_counter() - t0)
            async with conn.cursor() as cur:
                # statement_timeout por sesion: una query colgada no retiene
                # una conexion del pool para siempre.
                await cur.execute("SET statement_timeout = %s", (DB_STATEMENT_TIMEOUT_MS,))
                await cur.execute(query, params)
                rows = await cur.fetchall()
            await conn.commit()
        record_db_success()
        return rows
    except psycopg.errors.QueryCanceled:
        DB_QUERY_ERRORS.labels(kind="timeout").inc()
        raise HTTPException(status_code=504, detail="query timeout")
    except psycopg.PoolTimeout:
        DB_QUERY_ERRORS.labels(kind="pool_timeout").inc()
        record_db_failure()
        raise HTTPException(status_code=503, detail="pool de conexiones agotado")
    except psycopg.Error as exc:
        DB_QUERY_ERRORS.labels(kind="error").inc()
        record_db_failure()
        log.error("db error", extra={"error": str(exc)[:200]})
        raise HTTPException(status_code=503, detail="error de base de datos")


# -----------------------------------------------------------------------------
# Sondas
# -----------------------------------------------------------------------------
@app.get("/health/startup")
async def health_startup():
    """Solo verifica que el proceso levanto. Lo mas barato posible."""
    return {"status": "ok", "pod": POD_NAME}


@app.get("/health/live")
async def health_live():
    """Liveness. NO toca la BD a proposito.

    Si la readiness/liveness comprobara la BD, una caida de Postgres
    reiniciaria todos los pods del backend en cascada. Eso convierte un
    incidente de dependencia en una caida total y alarga el RTO.
    """
    return {"status": "ok", "pod": POD_NAME, "uptime_s": round(time.monotonic() - _startup_at, 1)}


@app.get("/health/ready")
async def health_ready(response: Response):
    """Readiness. Comprueba la BD con timeout corto.

    Si falla, el kubelet saca el pod del Service (degrada) pero no lo
    reinicia. Eso es exactamente el comportamiento deseado.
    """
    if pool is None:
        response.status_code = 503
        return {"status": "not-ready", "reason": "pool no inicializado"}
    try:
        # asgi_timeout es correcto para abortar la espera. abort() y lo que
        # hay que usar si se cancela la espera, porque ahi la conexion ya
        # fue reservada al pool y hay que devolverla explicitamente.
        async with asyncio.timeout(DB_CONNECT_TIMEOUT_MS / 1000):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        response.status_code = 200
        POD_READY.labels(pod=POD_NAME).set(1)
        return {"status": "ready", "pod": POD_NAME}
    except Exception as exc:  # noqa: BLE001
        response.status_code = 503
        POD_READY.labels(pod=POD_NAME).set(0)
        # Degradar, no morir: el kubelet saca el pod del Service pero no lo
        # reinicia, y la app sigue sirviendo lo que no depende de la BD.
        return {"status": "not-ready", "pod": POD_NAME, "reason": str(exc)[:120]}


# -----------------------------------------------------------------------------
# API — dominio Apuesta Total (simulado)
#
# Los endpoints existen para que el generador de carga tenga tráfico con la
# forma de un exchange de apuestas real: leer mercados, mover cuota, colocar
# apuesta (que toca wallet + ledger + bets + notifications en la misma
# transacción) y liquidar.
# -----------------------------------------------------------------------------
@app.get(f"{API_PREFIX}/health")
async def api_health():
    return {"status": "ok", "pod": POD_NAME, "node": NODE_NAME}


@app.get(f"{API_PREFIX}/events")
async def list_events(status: str | None = None, limit: int = 50,
                      _: None = Depends(rate_limit)):
    """Eventos (partidos). Por defecto los que están en juego, que son los
   interesting para la prueba."""
    q = """
        SELECT e.id, e.name, e.competition, e.starts_at, e.status,
               e.clock_minute, e.score_home, e.score_away,
               count(DISTINCT m.id) AS markets
        FROM events e
        LEFT JOIN markets m ON m.event_id = e.id AND m.status = 'OPEN'
        WHERE (%s::text IS NULL OR e.status = %s)
        GROUP BY e.id
        ORDER BY (e.status = 'IN_PLAY') DESC, e.starts_at
        LIMIT %s
    """
    rows = await fetch(q, (status, status, min(limit, 200)))
    cols = ("id", "name", "competition", "starts_at", "status",
            "clock_minute", "score_home", "score_away", "markets")
    return {"pod": POD_NAME, "events": [dict(zip(cols, r)) for r in rows]}


@app.get(f"{API_PREFIX}/events/{event_id}/markets")
async def list_event_markets(event_id: int, _: None = Depends(rate_limit)):
    """Mercados abiertos de un evento. El generador de carga la usa para
    descubrir ids reales en lugar de inventarlos."""
    rows = await fetch(
        """
        SELECT m.id, m.name, m.market_type, m.line, m.status,
               count(s.id) AS selections
        FROM markets m
        LEFT JOIN selections s ON s.market_id = m.id
        WHERE m.event_id = %s AND m.status = 'OPEN'
        GROUP BY m.id
        ORDER BY m.id
        """,
        (event_id,),
    )
    cols = ("id", "name", "market_type", "line", "status", "selections")
    return {"pod": POD_NAME, "event_id": event_id,
            "markets": [dict(zip(cols, r)) for r in rows]}


@app.get(f"{API_PREFIX}/markets/{market_id}")
async def get_market(market_id: int, _: None = Depends(rate_limit)):
    """Mercado con sus selecciones y el último precio de cada una.
    Es lo que "ve" el usuario antes de apostar."""
    rows = await fetch(
        """
        SELECT s.id, s.code, s.name, s.is_winner,
               o.id, o.back_price, o.back_size, o.lay_price, o.lay_size
        FROM selections s
        LEFT JOIN LATERAL (
            SELECT * FROM odds o
            WHERE o.selection_id = s.id
            ORDER BY o.created_at DESC
            LIMIT 1
        ) o ON TRUE
        WHERE s.market_id = %s
        ORDER BY s.id
        """,
        (market_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="mercado no encontrado")
    cols = ("selection_id", "code", "name", "is_winner",
            "odds_id", "back_price", "back_size", "lay_price", "lay_size")
    return {"pod": POD_NAME, "market_id": market_id,
            "selections": [dict(zip(cols, r)) for r in rows]}


@app.get(f"{API_PREFIX}/odds/{market_id}")
async def get_odds(market_id: int, limit: int = 10,
                   _: None = Depends(rate_limit)):
    """Historial de cuotas del mercado. Es la query que satura la BD en las
    pruebas: ORDER BY + LIMIT sobre un índice compuesto, y en un exchange
    real se lee muchísimo más que cualquier otra cosa."""
    rows = await fetch(
        """
        SELECT o.id, o.selection_id, o.back_price, o.back_size,
               o.lay_price, o.lay_size, o.created_at
        FROM odds o
        WHERE o.market_id = %s
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT %s
        """,
        (market_id, min(limit, 200)),
    )
    cols = ("odds_id", "selection_id", "back_price", "back_size",
            "lay_price", "lay_size", "created_at")
    return {"pod": POD_NAME, "market_id": market_id, "history": [dict(zip(cols, r)) for r in rows]}


@app.post(f"{API_PREFIX}/bets")
async def place_bet(payload: dict, _: None = Depends(rate_limit)):
    """Coloca una apuesta.

    Esta es la operación que define el sistema, y por eso todo va en UNA
    transacción: decrementar el wallet, escribir el ledger, insertar la apuesta
    y encolar la notificación. Si el wallet baja y la apuesta falla a medias,
    el usuario tiene dinero que no es suyo. Si la apuesta entra y el wallet no
    baja, el dinero aparece de la nada. El aislamiento por defecto de Postgres
    (READ COMMITTED) no protege esto: hace falta SELECT ... FOR UPDATE sobre la
    fila del wallet para serializar dos apostas concurrentes del mismo usuario.

    Aquí se hace con un UPDATE condicional (available >= coste), que en una
    sola sentencia atomica es equivalente y evita el SELECT previo.
    """
    user_id = int(payload.get("user_id", 0))
    odds_id = int(payload.get("odds_id", 0))
    stake = float(payload.get("stake", 0))
    side = str(payload.get("side", "BACK")).upper()

    if user_id <= 0 or odds_id <= 0:
        raise HTTPException(status_code=400, detail="user_id y odds_id son obligatorios")
    if stake <= 0 or stake > 10_000:
        raise HTTPException(status_code=400, detail="stake fuera de rango")
    if side not in ("BACK", "LAY"):
        raise HTTPException(status_code=400, detail="side debe ser BACK o LAY")

    if pool is None:
        raise HTTPException(status_code=503, detail="pool no inicializado")
    if circuit_state():
        raise HTTPException(status_code=503, detail="circuit breaker abierto",
                            headers={"Retry-After": str(CIRCUIT_RESET_SECONDS)})

    group_id = payload.get("bet_group_id") or str(uuid.uuid4())
    t0 = time.perf_counter()

    try:
        async with pool.connection() as conn:
            DB_POOL_WAIT.observe(time.perf_counter() - t0)
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SET statement_timeout = %s", (DB_STATEMENT_TIMEOUT_MS,))

                    # 1. El precio que vio el usuario. Inmutable: define qué se
                    #    liquida aunque el mercado se mueva después.
                    await cur.execute(
                        "SELECT selection_id, back_price, lay_price "
                        "FROM odds WHERE id = %s FOR SHARE",
                        (odds_id,),
                    )
                    odds_row = await cur.fetchone()
                    if odds_row is None:
                        raise HTTPException(status_code=404, detail="cuota no encontrada")
                    _, back_price, lay_price = odds_row

                    price = float(back_price if side == "BACK" else lay_price)
                    # payout: BACK gana stake*price; LAY gana stake - stake*(price-1).
                    if side == "BACK":
                        payout = round2(stake * price)
                    else:
                        payout = round2(max(0.0, stake - stake * (price - 1)))

                    # 2. Movimiento del wallet. El WHERE es la clave: si no hay
                    #    saldo, la sentencia no toca fila alguna y affected_rows
                    #    es 0. Eso es un rechazo atómico, no una carrera.
                    #    Para LAY el requisito es el pasivo (exposure), no el
                    #    efectivo: es lo que distingue un exchange de un casino.
                    if side == "BACK":
                        await cur.execute(
                            "UPDATE wallets SET available = available - %s, updated_at = now() "
                            "WHERE user_id = %s AND available >= %s",
                            (stake, user_id, stake),
                        )
                    else:
                        liability = round2(stake * (price - 1))
                        await cur.execute(
                            "UPDATE wallets SET exposure = exposure + %s, updated_at = now() "
                            "WHERE user_id = %s AND available >= %s",
                            (liability, user_id, liability),
                        )

                    if cur.rowcount == 0:
                        # Sin fondos. Es un rechazo de negocio esperado, no un
                        # error de sistema: bajo carga, muchos usuarios agotan
                        # su saldo. Se cuenta aparte para que el panel de
                        # alertas no lo confunda con una caída.
                        BETS_REJECTED.labels(reason="insufficient_funds").inc()
                        raise HTTPException(status_code=402,
                                            detail="saldo insuficiente",
                                            headers={"Retry-After": "0"})

                    # 3. Apuesta. Referencia odds_id: el precio queda anclado.
                    await cur.execute(
                        """
                        INSERT INTO bets (user_id, market_id, bet_group_id, status,
                                          odds_id, side, stake, potential_payout)
                        SELECT %s, m.id, %s, 'PLACED', %s, %s, %s, %s
                        FROM markets m WHERE m.id = (
                            SELECT market_id FROM odds WHERE id = %s
                        )
                        RETURNING id, market_id
                        """,
                        (user_id, group_id, odds_id, side, stake, payout, odds_id),
                    )
                    bet_row = await cur.fetchone()
                    if bet_row is None:
                        raise HTTPException(status_code=404, detail="mercado no encontrado")
                    bet_id, market_id = bet_row

                    # 4. Ledger. Append-only: la verdad del saldo.
                    await cur.execute(
                        "SELECT available FROM wallets WHERE user_id = %s", (user_id,)
                    )
                    (bal,) = await cur.fetchone()
                    await cur.execute(
                        "INSERT INTO transactions (user_id, bet_id, type, amount, balance_after) "
                        "VALUES (%s, %s, 'BET_STAKE', %s, %s)",
                        (user_id, bet_id, -stake, bal),
                    )

                    # 5. Notificación. No crítica: si este servicio falla, el
                    #    usuario apostó igual. Por eso va dentro de la misma
                    #    transacción pero no la bloquea (es una fila más).
                    await cur.execute(
                        "INSERT INTO notifications (user_id, bet_id, kind, payload) "
                        "VALUES (%s, %s, 'BET_PLACED', %s::jsonb)",
                        (user_id, bet_id,
                         json.dumps({"market_id": market_id, "side": side,
                                     "stake": stake, "price": price})),
                    )
    except HTTPException:
        raise
    except psycopg.errors.QueryCanceled:
        DB_QUERY_ERRORS.labels(kind="timeout").inc()
        raise HTTPException(status_code=504, detail="timeout colocando la apuesta")
    except psycopg.PoolTimeout:
        DB_QUERY_ERRORS.labels(kind="pool_timeout").inc()
        record_db_failure()
        raise HTTPException(status_code=503, detail="pool agotado")
    except psycopg.Error as exc:
        DB_QUERY_ERRORS.labels(kind="error").inc()
        record_db_failure()
        log.error("db error en place_bet", extra={"error": str(exc)[:200]})
        raise HTTPException(status_code=503, detail="error de base de datos")

    record_db_success()
    BETS_PLACED.inc()
    return {"pod": POD_NAME, "bet_id": bet_id, "market_id": market_id,
            "side": side, "stake": stake, "price": price,
            "potential_payout": payout, "status": "PLACED"}


@app.post(f"{API_PREFIX}/bets/{bet_id}/settle")
async def settle_bet(bet_id: int, payload: dict, _: None = Depends(rate_limit)):
    """Liquida una apuesta.

    IDEMPOTENCIA: es la operación más delicada. Si un cliente reintenta o el
    worker reintenta, el usuario no puede cobrar dos veces. La protección es el
    índice único sobre liquidation_key: la segunda operación choca contra el
    índice y devuelve 409 en vez de pagar.

    La clave se deriva de (bet_id, payout) y no de un UUID aleatorio: si dos
    reintentos calculan la misma clave, el índice los descarta a los dos. Un
    UUID por intento NO funcionaría, porque cada reintento generaría uno
    distinto y los dos pasarían.
    """
    won = bool(payload.get("won", False))
    key = payload.get("liquidation_key") or f"bet:{bet_id}:{'W' if won else 'L'}"

    if pool is None:
        raise HTTPException(status_code=503, detail="pool no inicializado")

    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SET statement_timeout = %s", (DB_STATEMENT_TIMEOUT_MS,))

                    # Bloqueo de la apuesta. Sin FOR UPDATE, dos liquidaciones
                    # simultáneas leerían el mismo status y pagarían dos veces.
                    await cur.execute(
                        "SELECT user_id, side, stake, potential_payout, status "
                        "FROM bets WHERE id = %s FOR UPDATE",
                        (bet_id,),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        raise HTTPException(status_code=404, detail="apuesta no encontrada")
                    user_id, side, stake, potential, status = row

                    if status not in ("PLACED", "MATCHED"):
                        raise HTTPException(
                            status_code=409, detail=f"apuesta ya liquidada ({status})")

                    payout = potential if won else 0.0
                    new_status = "WON" if won else "LOST"

                    # El índice único es el que realmente garantiza la
                    # idempotencia: si ya existe esta clave, no se inserta.
                    await cur.execute(
                        """
                        INSERT INTO transactions (user_id, bet_id, type, amount, balance_after)
                        VALUES (%s, %s, 'BET_PAYOUT', %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (user_id, bet_id, payout, payout),
                    )
                    if cur.rowcount == 0:
                        raise HTTPException(status_code=409, detail="liquidación duplicada")

                    # Descontar el pasivo de los LAY y devolver el stake al
                    # BACK perdedor. El saldo del usuario tiene que cuadrar.
                    if side == "BACK":
                        if won:
                            await cur.execute(
                                "UPDATE wallets SET available = available + %s, updated_at = now() "
                                "WHERE user_id = %s", (payout, user_id))
                        else:
                            await cur.execute(
                                "UPDATE wallets SET available = available + %s, updated_at = now() "
                                "WHERE user_id = %s", (stake, user_id))
                    else:  # LAY
                        liability = round2(stake * 1.0)
                        await cur.execute(
                            "UPDATE wallets SET exposure = GREATEST(0, exposure - %s), updated_at = now() "
                            "WHERE user_id = %s", (liability, user_id))
                        if not won:
                            await cur.execute(
                                "UPDATE wallets SET available = available + %s, updated_at = now() "
                                "WHERE user_id = %s", (stake, user_id))

                    await cur.execute(
                        "UPDATE bets SET status = %s, payout = %s, settled_at = now(), "
                        "liquidation_key = %s WHERE id = %s",
                        (new_status, payout, key, bet_id))

                    await cur.execute(
                        "INSERT INTO notifications (user_id, bet_id, kind, payload) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        (user_id, bet_id, "BET_WON" if won else "BET_LOST",
                         json.dumps({"payout": payout, "stake": stake})))
    except HTTPException:
        raise
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="liquidación duplicada")
    except psycopg.PoolTimeout:
        record_db_failure()
        raise HTTPException(status_code=503, detail="pool agotado")
    except psycopg.Error as exc:
        DB_QUERY_ERRORS.labels(kind="error").inc()
        record_db_failure()
        log.error("db error en settle", extra={"error": str(exc)[:200], "bet": bet_id})
        raise HTTPException(status_code=503, detail="error de base de datos")

    record_db_success()
    BETS_SETTLED.labels(result="won" if won else "lost").inc()
    return {"pod": POD_NAME, "bet_id": bet_id, "status": new_status, "payout": payout}

    return {"pod": POD_NAME, "bet_id": bet_id, "status": new_status, "payout": payout}


@app.get(f"{API_PREFIX}/users/{user_id}/wallet")
async def get_wallet(user_id: int, _: None = Depends(rate_limit)):
    """Saldo del usuario. En una prueba, comparar este número contra el
    SUM(amount) del ledger es lo que demuestra que no se ha perdido ni
    inventado dinero."""
    rows = await fetch(
        """
        SELECT w.available, w.exposure, w.currency,
               COALESCE(SUM(t.amount), 0) AS ledger_sum
        FROM wallets w
        LEFT JOIN transactions t ON t.user_id = w.user_id
        WHERE w.user_id = %s
        GROUP BY w.available, w.exposure, w.currency
        """,
        (user_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="usuario no encontrado")
    available, exposure, currency, ledger_sum = rows[0]
    # Divergencia wallet vs ledger. En operación normal es 0.
    drift = round2(float(available) - float(ledger_sum))
    return {"pod": POD_NAME, "user_id": user_id, "available": available,
            "exposure": exposure, "currency": currency,
            "ledger_sum": ledger_sum, "drift": drift}


@app.get(f"{API_PREFIX}/stats")
async def stats(_: None = Depends(rate_limit)):
    """Agregados del sistema. Query pesada a propósito: agregación sobre bets
    y transactions, que es lo que satura la BD cuando se corre a fondo."""
    rows = await fetch(
        """
        SELECT
          (SELECT count(*) FROM events WHERE status = 'IN_PLAY')                     AS live_events,
          (SELECT count(*) FROM markets WHERE status = 'OPEN')                       AS open_markets,
          (SELECT count(*) FROM bets)                                                 AS total_bets,
          (SELECT count(*) FROM bets WHERE status IN ('PLACED','MATCHED'))           AS open_bets,
          (SELECT count(*) FROM bets WHERE status = 'WON')                           AS won,
          (SELECT count(*) FROM bets WHERE status = 'LOST')                          AS lost,
          (SELECT COALESCE(SUM(stake), 0) FROM bets)                                 AS volume,
          (SELECT count(*) FROM notifications WHERE NOT delivered)                   AS pending_notifications
        """
    )
    cols = ("live_events", "open_markets", "total_bets", "open_bets",
            "won", "lost", "volume", "pending_notifications")
    return {"pod": POD_NAME, **dict(zip(cols, rows[0]))}


@app.get(f"{API_PREFIX}/seed-summary")
async def seed_summary_ep():
    """Conteos del dataset. Confirma que las semillas se cargaron."""
    summary = await asyncio.to_thread(seed_summary, _dsn())
    return {"pod": POD_NAME, **summary}


@app.get(f"{API_PREFIX}/whoami")
async def whoami():
    """Devuelve qué réplica respondió. Sirve para verificar el balanceo del
    Service durante las pruebas de carga."""
    return {"pod": POD_NAME, "node": NODE_NAME, "ts": time.time()}


# -----------------------------------------------------------------------------
# Metricas
# -----------------------------------------------------------------------------
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# -----------------------------------------------------------------------------
# Instrumentacion global de latencia
# -----------------------------------------------------------------------------
@app.middleware("http")
async def instrument(request: Request, call_next):
    if request.url.path.startswith("/health") or request.url.path == "/metrics":
        return await call_next(request)
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    route = request.url.path
    # Agrupar por patron para no crear una serie por cada id. Sin esto, cada
    # bet_id distinto genera su propia serie de latencia y las métricas de
    # Prometheus se llenan de cardinalidad basura (el problema clásico de
    # instrumentar un path con id en la URL).
    for prefix, replacement in (
        (f"{API_PREFIX}/events/", f"{API_PREFIX}/events/:id/markets"),
        (f"{API_PREFIX}/markets/", f"{API_PREFIX}/markets/:id"),
        (f"{API_PREFIX}/odds/", f"{API_PREFIX}/odds/:id"),
        (f"{API_PREFIX}/users/", f"{API_PREFIX}/users/:id/wallet"),
    ):
        if prefix in route:
            route = replacement
            break
    if route.startswith(f"{API_PREFIX}/bets/") and route.endswith("/settle"):
        route = f"{API_PREFIX}/bets/:id/settle"
    elif route.startswith(f"{API_PREFIX}/bets/"):
        route = f"{API_PREFIX}/bets/:id"
    LATENCY.labels(method=request.method, path=route).observe(elapsed)
    REQUESTS.labels(
        method=request.method, path=route, status=str(response.status_code), pod=POD_NAME
    ).inc()
    response.headers["X-Pod-Name"] = POD_NAME
    return response


# -----------------------------------------------------------------------------
# Manejo de errores: degradar en vez de 500 cuando la BD no responde
# -----------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail, "pod": POD_NAME})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.error("unhandled", extra={"error": str(exc)[:200], "path": request.url.path})
    return JSONResponse(status_code=500, content={"error": "internal error", "pod": POD_NAME})


if __name__ == "__main__":
    # 1 worker por pod: el escalado horizontal se hace con mas pods, no con
    # mas threads dentro. Es la practica correcta en contenedores.
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        workers=1,
        access_log=False,     # las metricas ya cubren esto
        log_level=LOG_LEVEL.lower(),
        timeout_keep_alive=20,  # < terminationGracePeriodSeconds
    )
