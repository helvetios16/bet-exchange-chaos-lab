#!/usr/bin/env bash
# Muestra en vivo el comportamiento del HPA mientras corre k6 (requisito 4.8).
# Terminal 1:  ./scripts/demo-hpa.sh
# Terminal 2:  k6 run -e BASE_URL=http://<balanceador> tests/load/k6-load.js
# Guarda la serie temporal en reports/hpa-<fecha>.csv como evidencia.
set -uo pipefail
mkdir -p reports
OUT="reports/hpa-$(date +%Y%m%d-%H%M%S).csv"
echo "hora,replicas_disponibles,replicas_deseadas,cpu_actual_pct,memoria_actual_pct" | tee "$OUT"
while true; do
  read -r avail desired <<<"$(kubectl -n kuber-app get hpa kuber-backend-hpa \
    -o jsonpath='{.status.currentReplicas} {.status.desiredReplicas}')"
  cpu="$(kubectl -n kuber-app get hpa kuber-backend-hpa \
    -o jsonpath='{.status.currentMetrics[?(@.resource.name=="cpu")].resource.current.averageUtilization}')"
  mem="$(kubectl -n kuber-app get hpa kuber-backend-hpa \
    -o jsonpath='{.status.currentMetrics[?(@.resource.name=="memory")].resource.current.averageUtilization}')"
  echo "$(date +%H:%M:%S),${avail},${desired},${cpu},${mem}" | tee -a "$OUT"
  sleep 10
done
