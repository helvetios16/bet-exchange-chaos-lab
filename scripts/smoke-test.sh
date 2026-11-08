#!/usr/bin/env bash
# Comprueba que la aplicación desplegada responde. Reintenta: el DNS del
# balanceador tarda un par de minutos en propagarse tras crearlo.
set -euo pipefail

URL="${1:-}"
if [ -z "$URL" ]; then
  LB="$(kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  URL="http://${LB}"
fi

check() {
  local path="$1" expect="$2" code=""
  for _ in $(seq 1 40); do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${URL}${path}" || true)"
    if [ "$code" = "$expect" ]; then echo "OK   ${path} -> ${code}"; return 0; fi
    sleep 6
  done
  echo "FALLA ${path} -> ${code} (esperado ${expect})"
  return 1
}

check /api/v1/health 200
check /api/v1/events 200
check / 200
echo "Smoke test superado en ${URL}"
