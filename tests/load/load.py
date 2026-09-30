#!/usr/bin/env python3
# =============================================================================
# Generador de carga para las pruebas de escalabilidad.
#
# Por qué está en Python y no usando una herramienta estándar (k6, Locust):
#   - el objetivo es medir el efecto del HPA, así que hace falta controlar
#     la tasa de requests con precisión y reporta métricas por segundo
#   - sin dependencias externas: corre en un python:3.12-slim pelado
#   - el propio generador informa del comportamiento del sistema, así que el
#     informe de la prueba y la carga salen del mismo sitio
#
# Modelo de carga: rampa lineal de 0 a MAX_RPS durante RAMP_SECONDS, luego
# mantenimiento. Se reportan los valores por segundo en formato JSONL para que
# se pueda graficar después.
# =============================================================================
import asyncio
import json
import os
import random
import signal
import sys
import time
from collections import defaultdict

import aiohttp

TARGET = os.getenv("TARGET", "http://localhost:8080/api/v1")
API = TARGET if TARGET.endswith("/api/v1") else f"{TARGET}/api/v1"
RAMP_SECONDS = int(os.getenv("RAMP_SECONDS", "180"))
MAX_RPS = int(os.getenv("MAX_RPS", "500"))
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "120"))
WORKERS = int(os.getenv("WORKERS", "64"))
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
SEED = int(os.getenv("SEED", "1337"))

# Peso de cada operación. Se parece al tráfico real de un exchange: muchas
# lecturas de mercado y cuota, apuestas escalonadas, liquidaciones al final.
#
#   events   GET  /events            <- navegación: qué partidos hay
#   market   GET  /markets/{id}      <- ver las cuotas de un mercado
#   odds     GET  /odds/{id}         <- historial de cuotas (la query caliente)
#   bet      POST /bets              <- la operación que escribe: wallet+ledger+bet
#   settle   POST /bets/{id}/settle  <- liquidación (idempotente)
#   stats    GET  /stats             <- agregados, la más pesada de todas
#   whoami   GET  /whoami            <- trivial, línea base
#
# Los IDs no son fijos: se descubyen en la fase de descubrimiento contra los
# datos sembrados, porque las apuestas necesitan un market_id y un odds_id que
# existen de verdad. Inventar ids produce un 404 constante y mide el 404, no el
# sistema.
WEIGHTS = {
    "events": 20, "market": 25, "odds": 20, "bet": 20, "settle": 8, "stats": 3, "whoami": 4,
}
MAX_RPS_DEFAULT = 500

_running = True
_stats: dict = defaultdict(lambda: {"n": 0, "err": 0, "total_ms": 0.0, "max_ms": 0.0})
_by_status: dict = defaultdict(int)
_samples: list = []


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# -----------------------------------------------------------------------------
# Descubrimiento
#
# El generador no inventa ids: primero pregunta al sistema qué hay. Sin esto, una
# apuesta con un odds_id aleatorio devuelve 404 el 100% de las veces y la prueba
# mide el 404, no el sistema. Este arranque es lo que separa una prueba de carga
# de un bucle de errores.
# -----------------------------------------------------------------------------
class Discovery:
    def __init__(self):
        self.market_ids: list[int] = []
        self.odds_ids: list[int] = []
        self.user_ids: list[int] = []
        self.bet_ids: list[int] = []

    @property
    def ready(self) -> bool:
        return bool(self.market_ids and self.user_ids)


discovery = Discovery()


async def discover(session: aiohttp.ClientSession) -> None:
    """Fase de calentamiento: lee el dataset sembrado y guarda ids válidos."""
    log("descubriendo el dataset…")

    async with session.get(f"{API}/events?limit=100") as r:
        events = (await r.json()).get("events", [])
    if not events:
        raise RuntimeError(f"no hay eventos en {API}/events. ¿Se sembraron las semillas?")

    # Mercados de los primeros eventos (con markets > 0, o sea con mercado abierto)
    with_markets = [e for e in events if e.get("markets", 0) > 0][:8]
    for ev in with_markets:
        # El id de mercado no viene en /events; hay que entrar en cada mercado
        # del evento. Es un GET por mercado, suficiente para el arranque.
        async with session.get(f"{API}/events/{ev['id']}/markets") as r:
            if r.status != 200:
                continue
            for m in (await r.json()).get("markets", []):
                discovery.market_ids.append(m["id"])
                # De cada mercado, la última cuota de la primera selección
                async with session.get(f"{API}/markets/{m['id']}") as mr:
                    if mr.status != 200:
                        continue
                    sels = (await mr.json()).get("selections", [])
                    if sels and sels[0].get("odds_id"):
                        discovery.odds_ids.append(sels[0]["odds_id"])

    # Usuarios: deducidos del rango de ids sembrados. /seed-summary expone el
    # total, así que se usa el rango 1..N que es exactamente lo que genera seed.py.
    async with session.get(f"{API}/seed-summary") as r:
        if r.status == 200:
            n_users = (await r.json()).get("users", 0)
            discovery.user_ids = list(range(1, n_users + 1))

    if not discovery.market_ids:
        raise RuntimeError("no se encontró ningún mercado con cuotas. Revisa las semillas.")
    if not discovery.user_ids:
        raise RuntimeError("no se encontraron usuarios. Revisa las semillas.")

    log(f"descubrimiento ok: {len(discovery.market_ids)} mercados, "
        f"{len(discovery.odds_ids)} cuotas, {len(discovery.user_ids)} usuarios")


def pick_op() -> tuple[str, str, str, dict]:
    """Devuelve (nombre, método, path, payload) de una operación ponderada.

    Las operaciones de escritura necesitan ids reales del dataset, así que se
    construyen aquí y no en una tabla fija.
    """
    total = sum(WEIGHTS.values())
    r = random.uniform(0, total)
    upto = 0
    for name, weight in WEIGHTS.items():
        upto += weight
        if r <= upto:
            return _build(name)
    return _build("events")


def _build(name: str) -> tuple[str, str, str, dict]:
    if name == "events":
        return name, "GET", "/events?limit=50", {}
    if name == "market":
        mid = random.choice(discovery.market_ids)
        return name, "GET", f"/markets/{mid}", {}
    if name == "odds":
        mid = random.choice(discovery.market_ids)
        return name, "GET", f"/odds/{mid}?limit=20", {}
    if name == "bet":
        # Elige un odds_id descubierto y un usuario con saldo. Si el usuario
        # no tiene saldo, el backend responde 402 y eso se cuenta como rechazo
        # de negocio, no como error del sistema.
        odds_id = random.choice(discovery.odds_ids)
        uid = random.choice(discovery.user_ids)
        payload = {
            "user_id": uid,
            "odds_id": odds_id,
            "stake": round(random.uniform(1.0, 120.0), 2),
            "side": random.choices(["BACK", "LAY"], weights=[80, 20])[0],
        }
        return name, "POST", "/bets", payload
    if name == "settle":
        # Solo liquida apuestas que este mismo run ya ha creado.
        if not discovery.bet_ids:
            return "bet", "POST", "/bets", {
                "user_id": random.choice(discovery.user_ids),
                "odds_id": random.choice(discovery.odds_ids),
                "stake": round(random.uniform(1.0, 50.0), 2),
                "side": "BACK",
            }
        bid = random.choice(discovery.bet_ids)
        payload = {"won": random.random() < 0.5}
        return name, "POST", f"/bets/{bid}/settle", payload
    if name == "stats":
        return name, "GET", "/stats", {}
    return "whoami", "GET", "/whoami", {}


async def one_request(session: aiohttp.ClientSession, name: str, method: str,
                      path: str, payload: dict) -> None:
    url = f"{API}{path}"
    t0 = time.perf_counter()
    try:
        if method == "POST":
            async with session.post(url, json=payload, allow_redirects=False) as resp:
                body = await resp.read()
                _by_status[resp.status] += 1
                if resp.status >= 400:
                    _stats[name]["err"] += 1
                elif name == "bet" and resp.status < 300:
                    # Guardar el bet_id para poder liquidarlo después
                    try:
                        bet_id = json.loads(body)["bet_id"]
                        discovery.bet_ids.append(bet_id)
                        # La lista no crece sin límite: un pool acotado es
                        # suficiente y evita un consumo de memoria creciente.
                        if len(discovery.bet_ids) > 500:
                            discovery.bet_ids.pop(0)
                    except Exception:
                        pass
        else:
            async with session.get(url, allow_redirects=False) as resp:
                await resp.read()
                _by_status[resp.status] += 1
                if resp.status >= 400:
                    _stats[name]["err"] += 1
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        _stats[name]["err"] += 1
        _by_status[0] += 1
    finally:
        ms = (time.perf_counter() - t0) * 1000
        s = _stats[name]
        s["n"] += 1
        s["total_ms"] += ms
        s["max_ms"] = max(s["max_ms"], ms)


async def worker(session: aiohttp.ClientSession, stop: asyncio.Event, offset: float, period: float) -> None:
    """Un worker emite requests a una tasa fija, independientemente de lo que
    hagan los demás. El rate limiting global lo marca el orquestador, que va
    ajusta el número de workers en vez del sleep de cada uno: así la rampa
    es determinista y no depende del jitter de los schedulers."""
    next_t = time.perf_counter() + offset
    while not stop.is_set():
        now = time.perf_counter()
        if next_t > now:
            await asyncio.sleep(next_t - now)
        if stop.is_set():
            break
        name, method, path, payload = pick_op()
        # No se espera la request: se dispara. El límite de concurrencia lo
        # impone WORKERS y el connect_limit del connector.
        asyncio.create_task(one_request(session, name, method, path, payload))
        next_t += period


async def reporter(stop: asyncio.Event) -> None:
    """Un sample por segundo, con la tasa real de RPS y los percentiles."""
    last = {k: {"n": 0, "err": 0, "total_ms": 0.0} for k in WEIGHTS}
    last_status: dict = defaultdict(int)
    t_start = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(1.0)
        now = time.perf_counter()
        elapsed = int(now - t_start)

        rps = 0
        errs = 0
        lat_sum = 0.0
        for k, s in _stats.items():
            d_n = s["n"] - last[k]["n"]
            d_e = s["err"] - last[k]["err"]
            d_t = s["total_ms"] - last[k]["total_ms"]
            rps += d_n
            errs += d_e
            lat_sum += d_t
            last[k] = {"n": s["n"], "err": s["err"], "total_ms": s["total_ms"]}

        lat_mean = (lat_sum / rps) if rps else 0.0
        sample = {
            "t_s": elapsed,
            "rps": rps,
            "errors": errs,
            "err_pct": round(errs * 100 / rps, 2) if rps else 0.0,
            "lat_mean_ms": round(lat_mean, 1),
            "status": {str(k): v - last_status.get(k, 0) for k, v in _by_status.items()},
        }
        _samples.append(sample)
        print(json.dumps(sample), flush=True)

        if elapsed % 10 == 0:
            log(f"t={elapsed}s rps={rps} err={errs} ({sample['err_pct']}%) lat_mean={lat_mean:.0f}ms")
        last_status.update(dict(_by_status))


def build_stop() -> asyncio.Event:
    stop = asyncio.Event()

    def handler(signum, _frame):
        log(f"señal {signum} recibida, terminando… (ya se están despidiendo las requests)")
        _running = False
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return stop


async def main() -> None:
    random.seed(SEED)
    log(f"target={API}  rampa={RAMP_SECONDS}s hasta {MAX_RPS} rps  hold={HOLD_SECONDS}s  workers={WORKERS}")

    stop = build_stop()
    rep = asyncio.create_task(reporter(stop))

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    # connect_limit más alto que WORKERS: si no, los workers se bloquean en el
    # connect y el rate medido no es el rate pedido.
    connector = aiohttp.TCPConnector(limit=0, limit_per_host=0, force_close=False, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # Descubrimiento antes de generar tráfico. Si falla, es mejor que el
        # generador muera aquí con un mensaje claro que que corra 5 minutos
        # midiendo 404 sin que nadie entienda por qué.
        await discover(session)
        if not discovery.ready:
            raise RuntimeError("descubrimiento incompleto, no se puede generar carga útil")

        workers: list[asyncio.Task] = []
        total_phase = RAMP_SECONDS + HOLD_SECONDS
        t0 = time.perf_counter()
        # Ramp lineal: el número de workers activos crece con el tiempo. Cada
        # worker emite a una tasa fija calculada para que el conjunto dé la
        # curva de rampa.
        max_workers = max(1, WORKERS)
        for i in range(max_workers):
            workers.append(
                asyncio.create_task(worker(session, stop, offset=i * 0.0005, period=1.0 / MAX_RPS * max_workers))
            )
            # Retardo escalonado para que la rampa sea lineal en el tiempo:
            # el worker i entra en t = i * (RAMP/ max_workers)
            delay = RAMP_SECONDS / max_workers
            if not stop.is_set() and (time.perf_counter() - t0) < RAMP_SECONDS:
                await asyncio.sleep(delay)
                if i % 8 == 7:
                    log(f"  rampa: {i+1}/{max_workers} workers activos (t={int(time.perf_counter()-t0)}s)")

        # Mantenimiento
        if not stop.is_set():
            log(f"rampa completa, manteniendo {HOLD_SECONDS}s…")
            try:
                await asyncio.wait_for(stop.wait(), timeout=HOLD_SECONDS)
            except asyncio.TimeoutError:
                pass

        # Dejar drenar: se espera a que terminen las requests en vuelo antes de
        # cerrar la sesión, para no contarlas como errores falsos.
        if any(not w.done() for w in workers):
            log("drenando requests en vuelo…")
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.sleep(1.0)  # que el último sample del reporter salga

    stop.set()
    await rep

    # Resumen final por operación
    log("=== resumen por operación ===")
    total_n = sum(s["n"] for s in _stats.values())
    total_err = sum(s["err"] for s in _stats.values())
    for name, s in sorted(_stats.items()):
        if not s["n"]:
            continue
        log(
            f"  {name:7s} n={s['n']:7d} err={s['err']:6d} "
            f"({s['err']*100/s['n']:.2f}%) lat_media={s['total_ms']/s['n']:.0f}ms lat_max={s['max_ms']:.0f}ms"
        )
    log(f"  TOTAL   n={total_n} err={total_err} ({total_err*100/total_n if total_n else 0:.2f}%)")
    log(f"  estados: {dict(_by_status)}")

    # Volcar el CSV para las gráficas
    out = os.getenv("SUMMARY_PATH", "")
    if out:
        with open(out, "w") as fh:
            json.dump(
                {
                    "total_requests": total_n,
                    "total_errors": total_err,
                    "samples": _samples,
                },
                fh,
            )
        log(f"muestras guardadas en {out}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("interrumpido")
        sys.exit(130)
