"""Pruebas unitarias del generador de semillas (sin BD)."""
import seed


def test_mismo_prng_produce_los_mismos_usuarios():
    a = seed.SeedGenerator(1337).gen_users(25)
    b = seed.SeedGenerator(1337).gen_users(25)
    assert a == b


def test_semillas_distintas_producen_usuarios_distintos():
    a = seed.SeedGenerator(1).gen_users(25)
    b = seed.SeedGenerator(2).gen_users(25)
    assert a != b


def test_saldos_dentro_del_rango():
    _, wallets = seed.SeedGenerator(7).gen_users(100, min_balance=100.0, max_balance=2500.0)
    assert len(wallets) == 100
    assert all(100.0 <= w["available"] <= 2500.0 for w in wallets)
    assert all(w["exposure"] == 0.0 for w in wallets)


def test_eventos_hot_quedan_in_play():
    events = seed.SeedGenerator(1337).gen_events(10, 3)
    assert len(events) == 10
    assert sum(1 for e in events if e["status"] == "IN_PLAY") == 3


def test_las_cuotas_cumplen_la_restriccion_de_la_bd():
    """schema.sql exige back_price > 1 y lay_price > 1 (CHECK)."""
    gen = seed.SeedGenerator(1337)
    events = gen.gen_events(6, 2)
    markets, selections = gen.gen_markets(events, 4)
    odds = gen.gen_odds(events, markets, selections, 3)
    assert odds
    assert all(o["back_price"] > 1 and o["lay_price"] > 1 for o in odds)
