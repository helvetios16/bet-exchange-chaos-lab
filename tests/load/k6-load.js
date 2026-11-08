// Prueba de carga con k6 contra el balanceador de AWS.
//
//   k6 run -e BASE_URL=http://<balanceador> tests/load/k6-load.js
//   k6 run -e BASE_URL=http://<balanceador> -e PROFILE=spike tests/load/k6-load.js
//
// PROFILE=ramp   (por defecto) rampa de ~7 min hasta el pico, mantiene y baja.
// PROFILE=spike  salto brusco, como el inicio de un partido.
//
// Mezcla de tráfico parecida a un exchange: mucha lectura de mercados y cuotas,
// y una parte de apuestas (que escriben en 4 tablas).
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
const API = `${BASE}/api/v1`;
const PROFILE = __ENV.PROFILE || 'ramp';

const profiles = {
  ramp: [
    { duration: '1m', target: 30 },
    { duration: '2m', target: 150 },
    { duration: '2m', target: 300 },
    { duration: '1m', target: 300 },
    { duration: '2m', target: 0 },
  ],
  spike: [
    { duration: '20s', target: 20 },
    { duration: '10s', target: 400 },
    { duration: '3m', target: 400 },
    { duration: '1m', target: 0 },
  ],
};

export const options = {
  scenarios: {
    mercado: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: profiles[PROFILE],
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // Las apuestas rechazadas por saldo (402) son esperadas; no cuentan como fallo.
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<1500'],
  },
};

// Descubre ids reales (eventos en juego -> mercados -> cuotas) una sola vez.
export function setup() {
  const events = http.get(`${API}/events?status=IN_PLAY&limit=10`).json('events') || [];
  const odds = [];
  const markets = [];
  for (const ev of events) {
    const mk = http.get(`${API}/events/${ev.id}/markets`).json('markets') || [];
    for (const m of mk.slice(0, 2)) {
      markets.push(m.id);
      const sels = http.get(`${API}/markets/${m.id}`).json('selections') || [];
      for (const s of sels) if (s.odds_id) odds.push(s.odds_id);
    }
  }
  if (odds.length === 0) throw new Error('No hay cuotas: ¿se cargaron las semillas?');
  return { markets, odds };
}

export default function (data) {
  const r = Math.random();
  if (r < 0.40) {
    const res = http.get(`${API}/events?status=IN_PLAY`);
    check(res, { 'eventos 200': (x) => x.status === 200 });
  } else if (r < 0.65) {
    const m = data.markets[Math.floor(Math.random() * data.markets.length)];
    const res = http.get(`${API}/markets/${m}`);
    check(res, { 'mercado 200': (x) => x.status === 200 });
  } else if (r < 0.80) {
    const m = data.markets[Math.floor(Math.random() * data.markets.length)];
    const res = http.get(`${API}/odds/${m}?limit=20`);
    check(res, { 'cuotas 200': (x) => x.status === 200 });
  } else if (r < 0.95) {
    const userId = 1 + Math.floor(Math.random() * 300);
    const oddsId = data.odds[Math.floor(Math.random() * data.odds.length)];
    const res = http.post(
      `${API}/bets`,
      JSON.stringify({
        user_id: userId,
        odds_id: oddsId,
        stake: 1 + Math.floor(Math.random() * 5),
        side: Math.random() < 0.5 ? 'BACK' : 'LAY',
      }),
      { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /bets' } },
    );
    check(res, { 'apuesta 200/402': (x) => x.status === 200 || x.status === 402 });
  } else {
    const res = http.get(`${API}/stats`);
    check(res, { 'stats 200': (x) => x.status === 200 });
  }
  sleep(Math.random() * 0.3);
}
