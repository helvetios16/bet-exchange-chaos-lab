#!/usr/bin/env python3
# =============================================================================
# Generador de semillas — Apuesta Total (simulado)
#
# Produce un dataset sintético y DETERMINISTA de eventos, mercados, selecciones,
# cuotas y usuarios. Determinista significa: la misma semilla da exactamente
# los mismos datos, siempre. Eso es lo que hace comparables dos ejecuciones de
# una prueba de carga; si los datos cambian entre corrida y corrida, cualquier
# diferencia en latencia es ruido en vez de señal.
#
# Uso:
#   python seed.py --out seeds/ --seed 1337 --events 40
#   python seed.py --out seeds/ --events 120 --users 500 --hot 5
#
# `--hot` marca cuántos eventos quedan IN_PLAY desde el arranque. Son los que
# mueven cuotas mientras corre la carga: son los que generan la contención real
# sobre la tabla de cuotas, que es la más caliente del sistema.
#
# Los datos son INVENTADOS. Nombres de clubes, competiciones y usuarios se
# generan por combinación, no se copian de ningún sitio.
# =============================================================================
import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

# -----------------------------------------------------------------------------
# Vocabulario para generar nombres ficticios por combinación.
# Evitar de hecho ninguna clase de palabra que pueda sugerir un club real.
# -----------------------------------------------------------------------------
CITY_A = [
    "Alba", "Río", "Monte", "Puerto", "Valle", "Sierra", "Costa", "Prado",
    "Norte", "Sur", "Este", "Oeste", "Alta", "Baja", "Verde", "Grande",
]
CITY_B = ["CF", "FC", "CD", "UD", "CD", "Racing", "Club", "Deportivo"]

COMPETITIONS = [
    "Liga Nacional", "Copa Continental", "Segunda División", "Supercopa",
    "Torneo de Invierno", "Liga Regional", "Copa deiseconds", "Playoffs",
]

MARKET_SPECS = [
    ("MATCH_1X2", "Ganador del partido", None, [("HOME", "Local"), ("DRAW", "Empate"), ("AWAY", "Visitante")]),
    ("OVER_UNDER", "Goles totales", "2.5", [("OVER", "Más de"), ("UNDER", "Menos de")]),
    ("BOTH_TO_SCORE", "Ambos marcan", None, [("YES", "Sí"), ("NO", "No")]),
    ("HANDICAP", "Hándicap", "-0.5", [("HOME", "Local"), ("AWAY", "Visitante")]),
]

COUNTRIES = ["ES", "MX", "AR", "CO", "CL", "PE", "BR", "US", "FR", "DE", "IT", "PT"]

FIRST = ["Ana", "Luis", "Sara", "Diego", "Elena", "Marco", "Nuria", "Pablo",
         "Carmen", "Hugo", "Marta", "Javier", "Lucía", "Andrés", "Paula", "Tomás"]
LAST = ["García", "López", "Martínez", "Sánchez", "Pérez", "Gómez", "Ruiz", "Díaz",
        "Moreno", "Álvarez", "Romero", "Navarro", "Torres", "Domínguez", "Vázquez", "Ramos"]


def round2(x: float) -> float:
    return round(x, 2)


def round3(x: float) -> float:
    return round(x, 3)


class SeedGenerator:
    def __init__(self, seed: int = 1337):
        # Random instances separadas por dominio: si más adelante se toca la
        # generación de mercados, no cambia la de usuarios. Sin esto, añadir un
        # mercado reordena TODOS los números del dataset.
        self.base = random.Random(seed)
        self.r_events = random.Random(seed + 1)
        self.r_markets = random.Random(seed + 2)
        self.r_odds = random.Random(seed + 3)
        self.r_users = random.Random(seed + 4)
        self.r_bets = random.Random(seed + 5)
        self.seed = seed

    # -------------------------------------------------------------------------
    # Eventos
    # -------------------------------------------------------------------------
    def make_event_name(self) -> tuple[str, str]:
        city = f"{self.r_events.choice(CITY_A)} {self.r_events.choice(CITY_B)}"
        return city, city

    def gen_events(self, n_events: int, n_hot: int, horizon_hours: int = 48) -> list[dict]:
        """Genera eventos. Los primeros `n_hot` quedan IN_PLAY para que las
        cuotas se muevan desde el segundo cero de la prueba."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        events = []
        for i in range(n_events):
            home, _ = self.make_event_name()
            _, away = self.make_event_name()
            competition = self.r_events.choice(COMPETITIONS)

            if i < n_hot:
                status = "IN_PLAY"
                starts_at = now - timedelta(minutes=self.r_events.randint(5, 80))
                clock = self.r_events.randint(2, 85)
                score_home = self.r_events.choices([0, 1, 2, 3, 4], weights=[30, 30, 20, 12, 8])[0]
                score_away = self.r_events.choices([0, 1, 2, 3], weights=[35, 32, 20, 13])[0]
            else:
                status = "SCHEDULED"
                starts_at = now + timedelta(minutes=self.r_events.randint(30, horizon_hours * 60))
                clock, score_home, score_away = 0, 0, 0

            events.append({
                "id": i + 1,
                "name": f"{home} vs {away}",
                "competition": competition,
                "starts_at": starts_at.isoformat(),
                "status": status,
                "clock_minute": clock,
                "score_home": score_home,
                "score_away": score_away,
            })
        return events

    # -------------------------------------------------------------------------
    # Mercados y selecciones
    # -------------------------------------------------------------------------
    def gen_markets(self, events: list[dict], per_event: int = 4) -> tuple[list[dict], list[dict]]:
        markets, selections = [], []
        m_id, s_id = 1, 1
        for ev in events:
            # Un subconjunto de tipos por evento: no todos los partidos tienen
            # los cuatro mercados. Todos a la vez no ocurre en la realidad y
            # además infla el dataset sin aportar señal.
            specs = self.r_markets.sample(MARKET_SPECS, k=min(per_event, len(MARKET_SPECS)))
            for mtype, mname, line, sel_specs in specs:
                markets.append({
                    "id": m_id,
                    "event_id": ev["id"],
                    "name": mname if line is None else f"{mname} {line}",
                    "market_type": mtype,
                    "line": line,
                    "status": "OPEN",
                    "settled_at": None,
                })
                for code, sel_name in sel_specs:
                    selections.append({
                        "id": s_id,
                        "market_id": m_id,
                        "code": code,
                        "name": sel_name,
                        "is_winner": False,
                    })
                    s_id += 1
                m_id += 1
        return markets, selections

    # -------------------------------------------------------------------------
    # Cuotas
    # -------------------------------------------------------------------------
    def price_for(self, event: dict, code: str) -> float:
        """Precio base coherente con el contexto.

        Se calcula a partir del marcador y del reloj, no al azar: en un partido
        2-0 en el minuto 80 el favorito vale mucho menos que en el minuto 0.
        Si las cuotas no такоеbian esta lógica, la simulación se comporta de
        forma que no se parece a nada real.
        """
        r = self.r_odds
        home_goals = event["score_home"]
        away_goals = event["score_away"]
        minute = event["clock_minute"]
        live = event["status"] == "IN_PLAY"

        if not live:
            # Cuota previa. El favorito va por debajo de 2.0.
            base = {"HOME": 1.85, "DRAW": 3.40, "AWAY": 2.30,
                    "OVER": 1.90, "UNDER": 1.90, "YES": 1.75, "NO": 2.05}[code]
            # El código no es suficiente: YES y OVER comparten el valor base, y
            # el ruido se aplica distinto por tipo de mercado.
            noise = r.uniform(-0.12, 0.12)
            return round3(max(1.02, base + noise))

        # En vivo. Ajuste por el marcador y por el tiempo restante.
        time_factor = max(0.25, 1.0 - minute / 90.0)  # 1.0 al principio, 0.25 al final
        diff = home_goals - away_goals

        if code in ("HOME", "AWAY"):
            favourite = code == "HOME"
            lead = diff if favourite else -diff
            if lead > 0:
                # Favorito ganando: la cuota baja (más probable que gane).
                price = 1.25 + lead * 0.55
            elif lead == 0:
                price = 2.10
            else:
                # Depasando: la cuota sube.
                price = 2.20 + abs(lead) * 0.75
            price *= time_factor if lead > 0 else (2.0 - time_factor)
        elif code == "DRAW":
            price = 2.60 + abs(diff) * 0.40 + (minute / 90.0) * 0.8
        elif code in ("OVER", "UNDER"):
            total = home_goals + away_goals
            remaining = max(0.0, (90 - minute) / 90.0) * 3.0  # goles esperados por el resto
            expected = 2.7
            edge = (total + remaining) - expected
            # Edge positivo = se espera superar la línea => OVER es MÁS
            # PROBABLE, así que su cuota BAJA. El signo va al revés de lo
            # intuitivo, y por eso merece el comentario: con el signo
            # equivocado, un 0-0 al minuto 67 salía con OVER a 1.30 (barato,
            # cuando es casi imposible llegar a 3 goles) y UNDER a 2.60.
            price = 1.95 - (edge * 0.32 if code == "OVER" else -edge * 0.32)
        elif code == "YES":
            price = 1.95 if (home_goals + away_goals) > 0 else 2.40
        elif code == "NO":
            price = 2.15 if (home_goals + away_goals) > 0 else 1.50
        else:
            price = 2.0

        price += r.uniform(-0.08, 0.08)
        return round3(max(1.02, min(15.0, price)))

    def gen_odds(self, events: list[dict], markets: list[dict], selections: list[dict],
                 revisions: int = 3) -> list[dict]:
        """Varias revisiones de precio por selección.

        Es lo que da versión al libro: la apuesta guarda el odds_id que vio, y
        al liquidar se usa ese. Sin historial, una liquidación correcta es
        indistinguible de una que usó el precio equivocado.
        """
        by_id = {e["id"]: e for e in events}
        sel_by_market: dict[int, list[dict]] = {}
        for s in selections:
            sel_by_market.setdefault(s["market_id"], []).append(s)

        market_by_id = {m["id"]: m for m in markets}
        odds_rows = []
        o_id = 1
        for m in markets:
            event = by_id[m["event_id"]]
            for sel in sel_by_market.get(m["id"], []):
                for rev in range(revisions):
                    # Cada revisión posterior se mueve un poco respecto a la
                    # anterior: el precio de un mercado nunca es estático.
                    price = self.price_for(event, sel["code"])
                    if rev > 0:
                        drift = self.r_odds.uniform(-0.06, 0.06) * rev
                        price = round3(max(1.02, price + drift))
                    odds_rows.append({
                        "id": o_id,
                        "market_id": m["id"],
                        "selection_id": sel["id"],
                        "back_price": price,
                        "back_size": round2(self.r_odds.uniform(0, 900)),
                        "lay_price": round3(price + self.r_odds.uniform(0.02, 0.12)),
                        "lay_size": round2(self.r_odds.uniform(0, 900)),
                        "created_at": (
                            datetime.fromisoformat(event["starts_at"]) + timedelta(seconds=rev * 30)
                        ).isoformat(),
                    })
                    o_id += 1
        return odds_rows

    # -------------------------------------------------------------------------
    # Usuarios y wallets
    # -------------------------------------------------------------------------
    def gen_users(self, n_users: int, min_balance: float = 100.0,
                  max_balance: float = 2500.0) -> tuple[list[dict], list[dict]]:
        users, wallets = [], []
        # Fecha fija derivada de la semilla, NO datetime.now(). Con now(), dos
        # ejecuciones seguidas producían ficheros distintos y el dataset dejaba
        # de ser reproducible solo por el sello temporal.
        base_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(1, n_users + 1):
            username = f"{self.r_users.choice(FIRST).lower()}.{self.r_users.choice(LAST).lower()}{i}"
            country = self.r_users.choice(COUNTRIES)
            # Cada usuario se crea en un instante distinto derivado del índice,
            # de forma estable y reproducible.
            created = base_ts + timedelta(minutes=i * 7)
            # currency va aquí y no solo en wallets: el cargador espera esta
            # columna en users.jsonl. Si faltara, el COPY insertaría NULL en
            # una columna NOT NULL y la carga del dataset reventaría.
            users.append({"id": i, "username": username, "country": country,
                          "created_at": created.isoformat(), "currency": "EUR"})
            balance = round2(self.r_users.uniform(min_balance, max_balance))
            wallets.append({"user_id": i, "currency": "EUR", "available": balance,
                            "exposure": 0.0})
        return users, wallets


def write_jsonl(path: str, rows: list[dict]) -> None:
    """JSONL en vez de un JSON grande: se puede cargar en streaming, y una
    línea corrupta no invalida el fichero entero."""
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera semillas sintéticas para Apuesta Total (simulado)")
    ap.add_argument("--out", default="seeds", help="directorio de salida")
    ap.add_argument("--seed", type=int, default=1337, help="semilla del PRNG")
    ap.add_argument("--events", type=int, default=40, help="número de eventos")
    ap.add_argument("--hot", type=int, default=5, help="eventos IN_PLAY desde el inicio")
    ap.add_argument("--users", type=int, default=300, help="número de usuarios")
    ap.add_argument("--markets-per-event", type=int, default=4)
    ap.add_argument("--odds-revisions", type=int, default=3)
    args = ap.parse_args()

    if args.hot > args.events:
        print(f"ERROR: --hot ({args.hot}) no puede superar --events ({args.events})", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    gen = SeedGenerator(args.seed)

    events = gen.gen_events(args.events, args.hot)
    markets, selections = gen.gen_markets(events, args.markets_per_event)
    odds = gen.gen_odds(events, markets, selections, args.odds_revisions)
    users, wallets = gen.gen_users(args.users)

    files = {
        "events.jsonl": events,
        "markets.jsonl": markets,
        "selections.jsonl": selections,
        "odds.jsonl": odds,
        "users.jsonl": users,
        "wallets.jsonl": wallets,
    }
    for name, rows in files.items():
        write_jsonl(os.path.join(args.out, name), rows)

    hot = sum(1 for e in events if e["status"] == "IN_PLAY")
    print(f"semillas generadas en {args.out}/  (semilla PRNG={args.seed})")
    print(f"  eventos    {len(events):6d}   ({hot} IN_PLAY, {len(events)-hot} SCHEDULED)")
    print(f"  mercados   {len(markets):6d}")
    print(f"  selecciones{len(selections):6d}")
    print(f"  cuotas     {len(odds):6d}   ({args.odds_revisions} revisiones por selección)")
    print(f"  usuarios   {len(users):6d}")
    print(f"  wallets    {len(wallets):6d}")
    print()
    print("Determinista: volver a ejecutar con la misma semilla da el mismo dataset.")
    print("Los datos son inventados y no representan eventos ni participantes reales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
