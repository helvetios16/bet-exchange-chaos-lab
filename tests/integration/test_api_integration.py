"""Pruebas de integración: API + PostgreSQL reales.

La invariante central del sistema es `drift == 0`: el saldo cacheado en
`wallets.available` debe coincidir siempre con SUM(transactions.amount).
Si una apuesta o una liquidación pierde o inventa dinero, drift deja de ser 0.
"""
import pytest

P = "/api/v1"


def wallet(api, user_id):
    r = api.get(f"{P}/users/{user_id}/wallet")
    assert r.status_code == 200
    return r.json()


def apostar(api, user_id, odds_id, stake, side="BACK"):
    return api.post(f"{P}/bets", json={
        "user_id": user_id, "odds_id": odds_id, "stake": stake, "side": side})


# ---------------------------------------------------------------- arranque
def test_las_semillas_se_cargaron(api):
    s = api.get(f"{P}/seed-summary").json()
    assert s["events"] == 6
    assert s["events_live"] == 3
    assert s["users"] == 30


def test_readiness_ok_con_la_bd_disponible(api):
    r = api.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


# ---------------------------------------------------------------- lectura
def test_listar_eventos_en_juego(api):
    events = api.get(f"{P}/events", params={"status": "IN_PLAY"}).json()["events"]
    assert len(events) == 3
    assert all(e["status"] == "IN_PLAY" for e in events)


def test_mercado_inexistente_da_404(api):
    assert api.get(f"{P}/markets/99999999").status_code == 404


def test_wallet_inexistente_da_404(api):
    assert api.get(f"{P}/users/99999999/wallet").status_code == 404


# ---------------------------------------------------------------- apuestas
def test_apuesta_back_descuenta_saldo_y_mantiene_drift_cero(api, mercado_en_juego, usuario):
    _, odds_ids = mercado_en_juego
    antes = wallet(api, usuario)
    r = apostar(api, usuario, odds_ids[0], 10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PLACED"
    despues = wallet(api, usuario)
    assert float(despues["available"]) == pytest.approx(float(antes["available"]) - 10)
    assert despues["drift"] == 0


def test_apuesta_lay_mantiene_drift_cero(api, mercado_en_juego, usuario):
    _, odds_ids = mercado_en_juego
    r = apostar(api, usuario, odds_ids[0], 10, side="LAY")
    assert r.status_code == 200, r.text
    w = wallet(api, usuario)
    assert w["drift"] == 0
    assert float(w["exposure"]) > 0


def test_saldo_insuficiente_da_402_y_no_toca_el_saldo(api, mercado_en_juego, usuario):
    _, odds_ids = mercado_en_juego
    antes = wallet(api, usuario)
    r = apostar(api, usuario, odds_ids[0], 10_000)  # saldos semilla: 100..2500
    assert r.status_code == 402
    despues = wallet(api, usuario)
    assert despues["available"] == antes["available"]
    assert despues["drift"] == 0


def test_cuota_inexistente_da_404(api, usuario):
    assert apostar(api, usuario, 99999999, 10).status_code == 404


def test_muchas_apuestas_seguidas_no_generan_drift(api, mercado_en_juego, usuario):
    _, odds_ids = mercado_en_juego
    for i in range(15):
        r = apostar(api, usuario, odds_ids[i % len(odds_ids)], 1, side="BACK" if i % 2 else "LAY")
        assert r.status_code in (200, 402), r.text
    assert wallet(api, usuario)["drift"] == 0


# ------------------------------------------------------------- liquidación
@pytest.mark.parametrize("side", ["BACK", "LAY"])
@pytest.mark.parametrize("won", [True, False])
def test_liquidar_no_pierde_ni_inventa_dinero(api, mercado_en_juego, usuario, side, won):
    _, odds_ids = mercado_en_juego
    bet = apostar(api, usuario, odds_ids[0], 10, side=side).json()
    r = api.post(f"{P}/bets/{bet['bet_id']}/settle", json={"won": won})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ("WON" if won else "LOST")
    assert wallet(api, usuario)["drift"] == 0


def test_liquidacion_es_idempotente(api, mercado_en_juego, usuario):
    """Reintentar una liquidación no puede pagar dos veces."""
    _, odds_ids = mercado_en_juego
    bet = apostar(api, usuario, odds_ids[0], 10).json()
    primera = api.post(f"{P}/bets/{bet['bet_id']}/settle", json={"won": True})
    saldo_tras_primera = wallet(api, usuario)["available"]
    segunda = api.post(f"{P}/bets/{bet['bet_id']}/settle", json={"won": True})
    assert primera.status_code == 200
    assert segunda.status_code == 409
    w = wallet(api, usuario)
    assert w["available"] == saldo_tras_primera
    assert w["drift"] == 0


def test_liquidar_apuesta_inexistente_da_404(api):
    r = api.post(f"{P}/bets/99999999/settle", json={"won": True})
    assert r.status_code == 404


# ------------------------------------------------------------------ métricas
def test_estadisticas_reflejan_las_apuestas(api, mercado_en_juego, usuario):
    _, odds_ids = mercado_en_juego
    antes = api.get(f"{P}/stats").json()["total_bets"]
    assert apostar(api, usuario, odds_ids[0], 5).status_code == 200
    assert api.get(f"{P}/stats").json()["total_bets"] == antes + 1
