# =============================================================================
# Cargador de semillas — vuelca los JSONL en Postgres
#
# Se ejecuta dentro del pod del backend durante el arranque, después de aplicar
# el esquema. Idempotente por construcción: hace TRUNCATE ... RESTART IDENTITY
# CASCADE antes de insertar, así que correrlo dos veces deja la BD en el mismo
# estado. Eso importa porque las pruebas de carga se reejecutan una y otra vez
# sobre el mismo cluster.
#
# COPY en lugar de INSERT fila a fila: para 40 eventos x 4 mercados x 3 revisiones
# son unos miles de filas, pero COPY las mete en una fracción del tiempo y no
# dispara un planner por fila.
# =============================================================================
import json
import os
import pathlib

import psycopg

SEED_DIR = os.getenv("SEED_DIR", "/app/seeds")

# Orden importante: respeta las FK.
TABLES = [
    # (fichero, tabla, columnas)
    ("users.jsonl", "users", ["id", "username", "country", "created_at", "currency"]),
    ("events.jsonl", "events", ["id", "name", "competition", "starts_at", "status",
                                "clock_minute", "score_home", "score_away"]),
    ("markets.jsonl", "markets", ["id", "event_id", "name", "market_type", "line",
                                  "status", "settled_at"]),
    ("selections.jsonl", "selections", ["id", "market_id", "code", "name", "is_winner"]),
    ("odds.jsonl", "odds", ["id", "market_id", "selection_id", "back_price", "back_size",
                            "lay_price", "lay_size", "created_at"]),
    ("wallets.jsonl", "wallets", ["user_id", "currency", "available", "exposure"]),
]


def _rows(path: pathlib.Path, extra_cols: list[str]) -> list[tuple]:
    """Lee el JSONL y proyecta al orden de columnas de la tabla."""
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            # Tolerante a columnas ausentes: si el generador no emite una
            # columna, se rellena con None en vez de reventar el COPY.
            rows.append(tuple(rec.get(c) for c in extra_cols))
    return rows


def seed_database(dsn: str, seed_dir: str = SEED_DIR, verbose: bool = True) -> dict[str, int]:
    """Carga todas las semillas. Devuelve el conteo por tabla."""
    counts: dict[str, int] = {}
    base = pathlib.Path(seed_dir)

    if not base.is_dir():
        raise FileNotFoundError(
            f"no existe el directorio de semillas {seed_dir}. "
            "Géneralas con: python seed.py --out seeds"
        )

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False

        # Orden de borrado inverso al de inserción. RESTART IDENTITY reinicia las
        # secuencias, necesario porque se insertan ids explícitos.
        with conn.cursor() as cur:
            cur.execute("""
                TRUNCATE notifications, transactions, bets, wallets, odds,
                         selections, markets, events, users
                RESTART IDENTITY CASCADE
            """)

        for fname, table, cols in TABLES:
            fpath = base / fname
            if not fpath.exists():
                if verbose:
                    print(f"  [seed] {fname}: no existe, se omite")
                continue
            rows = _rows(fpath, cols)
            if not rows:
                counts[table] = 0
                continue

            col_list = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))
            with cur.copy(f"COPY {table} ({col_list}) FROM STDIN") as cp:
                for row in rows:
                    cp.write_row(row)
            counts[table] = len(rows)
            if verbose:
                print(f"  [seed] {table}: {len(rows)} filas")

        conn.commit()

    return counts


def seed_summary(dsn: str) -> dict[str, int]:
    """Conteos de las tablas principales. Para el log de arranque y para el
    panel de control del frontend."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM events),
                  (SELECT count(*) FROM events WHERE status = 'IN_PLAY'),
                  (SELECT count(*) FROM markets WHERE status = 'OPEN'),
                  (SELECT count(*) FROM selections),
                  (SELECT count(*) FROM odds),
                  (SELECT count(*) FROM users),
                  (SELECT count(*) FROM bets),
                  (SELECT count(*) FROM transactions)
            """)
            ev, hot, open_mk, sel, odds, usr, bets, tx = cur.fetchone()
    return {
        "events": ev, "events_live": hot, "markets_open": open_mk,
        "selections": sel, "odds_versions": odds, "users": usr,
        "bets": bets, "transactions": tx,
    }


if __name__ == "__main__":
    import sys

    dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN")
    if not dsn:
        print("ERROR: falta DATABASE_URL", file=sys.stderr)
        sys.exit(1)
    result = seed_database(dsn, verbose=True)
    print("  [seed] total:", sum(result.values()), "filas")
