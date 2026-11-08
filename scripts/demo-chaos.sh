#!/usr/bin/env bash
# Demostración de recuperación ante fallos (requisito 4.7).
# Ejecutar con la aplicación desplegada y, idealmente, una prueba de carga
# corriendo en otra terminal. Cada paso deja evidencia en pantalla.
set -uo pipefail

LB="$(kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
URL="http://${LB}/api/v1/health"

probe() {  # lanza peticiones durante $1 segundos y cuenta resultados
  local secs="$1" ok=0 bad=0 end=$((SECONDS + secs))
  while [ $SECONDS -lt $end ]; do
    code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$URL" || echo 000)"
    if [ "$code" = "200" ]; then ok=$((ok+1)); else bad=$((bad+1)); fi
    sleep 0.2
  done
  echo "    peticiones OK=$ok  fallidas=$bad"
}

echo "=== Estado inicial ==="
kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend -o wide

echo; echo "=== 1. Se mata un pod del backend ==="
VICTIM="$(kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend -o jsonpath='{.items[0].metadata.name}')"
echo "    eliminando $VICTIM"
kubectl -n kuber-app delete pod "$VICTIM" --wait=false
probe 25
kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend

echo; echo "=== 2. Se mata el pod de Postgres (el backend degrada, no se reinicia) ==="
kubectl -n kuber-data delete pod kuber-postgres-0 --wait=false
for i in 1 2 3 4 5 6; do
  r="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://${LB}/api/v1/events" || echo 000)"
  echo "    /events -> $r   |  $(kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend --no-headers | awk '{print $2"/"$3"/r"$4}' | tr '\n' ' ')"
  sleep 8
done
kubectl -n kuber-data rollout status statefulset/kuber-postgres --timeout=180s
echo "    Postgres recuperado. Reinicios del backend (deben ser 0 por esta causa):"
kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend --no-headers | awk '{print "    "$1"  reinicios="$4}'

echo; echo "=== 3. Se comprueba la consistencia del dinero tras el fallo ==="
curl -s "http://${LB}/api/v1/users/1/wallet"; echo
echo "    (drift debe ser 0)"
