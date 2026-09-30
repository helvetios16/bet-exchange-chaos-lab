#!/usr/bin/env bash
# =============================================================================
# autopilot.sh — Piloto automático de pruebas de resiliencia
#
# Ejecuta los tres ejes pedidos contra el cluster, mide y deja el sistema como
# estaba. Diseñado para correr de forma no destructiva y repetible.
#
#   ./scripts/autopilot.sh all        # los tres ejes + resumen
#   ./scripts/autopilot.sh stress     # carga sostenida, curva de escalado
#   ./scripts/autopilot.sh chaos      # fallos de pod, nodo y dependencia
#   ./scripts/autopilot.sh soak       # endurance largo (default 30 min)
#
# Variables de entorno:
#   NS_APP=kuber-app   NS_DATA=kuber-data   TARGET=http://localhost:8080
#   LOAD_PODS=2       RAMP_SECONDS=180     SOAK_MINUTES=30
#   OUT_DIR=reports/  (se sobreescribe con el timestamp del run)
# =============================================================================
set -Eeuo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
NS_APP="${NS_APP:-kuber-app}"
NS_DATA="${NS_DATA:-kuber-data}"
NS_OBS="${NS_OBS:-kuber-observe}"
TARGET="${TARGET:-http://localhost:8080}"
API="${TARGET}/api/v1"
LOAD_PODS="${LOAD_PODS:-2}"
RAMP_SECONDS="${RAMP_SECONDS:-180}"
SOAK_MINUTES="${SOAK_MINUTES:-30}"
BACKEND_HPA="${BACKEND_HPA:-kuber-backend-hpa}"
BACKEND_DEPLOY="${BACKEND_DEPLOY:-kuber-backend}"
PG_STATEFULSET="${PG_STATEFULSET:-kuber-postgres}"

# timestamp para nombrar los artefactos del run
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-reports/run-${STAMP}}"

# Colores (solo si la salida es un TTY)
if [[ -t 1 ]]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[34m'
  C_0=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_0=""
fi

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
log()  { printf '%s[%s]%s %s\n' "$C_B" "$(date +%H:%M:%S)" "$C_0" "$*"; }
ok()   { printf '%s  OK  %s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s AVISO%s %s\n' "$C_Y" "$C_0" "$*"; }
err()  { printf '%s FALLA%s %s\n' "$C_R" "$C_0" "$*" >&2; }
die()  { err "$*"; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "falta la dependencia '$1'"; }

# Snapshot del estado del sistema en un instante.
# Lo que se guarda aquí es lo que permite reconstruir después qué pasó.
snapshot() {
  local tag="${1:-snapshot}"
  {
    echo "===== $tag  $(date -Is) ====="
    echo "--- réplicas backend (spec vs ready) ---"
    kubectl -n "$NS_APP" get deploy "$BACKEND_DEPLOY" \
      -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,UPDATED:.status.updatedReplicas' 2>/dev/null || true
    echo "--- HPA ---"
    kubectl -n "$NS_APP" get hpa "$BACKEND_HPA" 2>/dev/null || true
    echo "--- pods por fase ---"
    kubectl -n "$NS_APP" get pods -o wide 2>/dev/null || true
    echo "--- endpoints del service backend ---"
    kubectl -n "$NS_APP" get endpoints kuber-backend 2>/dev/null || true
    echo "--- eventos recientes (ordenados por última vez) ---"
    kubectl -n "$NS_APP" get events --sort-by=.lastTimestamp 2>/dev/null | tail -30 || true
    echo "--- reinicios de contenedores ---"
    kubectl -n "$NS_APP" get pods -o json 2>/dev/null \
      | jq -r '.items[] | .metadata.name as $p | .status.containerStatuses[]? | "\($p)  \(.name)  restarts=\(.restartCount)  last=\(.lastState.terminated.reason // "-")"' 2>/dev/null || true
    echo
  } >> "$OUT_DIR/cluster-${tag}.log"
}

# Estado del HPA en un número, para las gráficas.
hpa_replicas() {
  kubectl -n "$NS_APP" get hpa "$BACKEND_HPA" \
    -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo "?"
}

# curl con timeout curto, para que un sistema que se cuelga no bloquee el test.
# Devuelve: "http_code segundos"
probe() {
  local url="$1" timeout="${2:-5}"
  curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time "$timeout" "$url" 2>/dev/null || echo "000 99.0"
}

# -----------------------------------------------------------------------------
# Precondiciones
# -----------------------------------------------------------------------------
preflight() {
  log "Comprobando precondiciones…"
  need kubectl
  need curl
  need jq

  kubectl cluster-info >/dev/null 2>&1 || die "no hay cluster accesible. ¿kind create cluster --config cluster/kind-config.yaml?"

  kubectl -n "$NS_APP" get deploy "$BACKEND_DEPLOY" >/dev/null 2>&1 \
    || die "no existe el deployment $BACKEND_DEPLOY en $NS_APP"

  # metrics-server es obligatorio para el HPA de CPU/memoria. Sin él, el HPA
  # se queda en <unknown> y el eje de escalabilidad no se puede medir.
  if ! kubectl get --raw /apis/metrics.k8s.io/v1beta1 2>/dev/null | grep -q nodeMetrics; then
    warn "metrics-server no disponible: el HPA de CPU no podrá medir."
    warn "  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
  else
    ok "metrics-server disponible (el HPA podrá escalar)"
  fi

  # ¿La app responde?
  local code
  code="$(probe "$API/health" 5 | cut -d' ' -f1)"
  [[ "$code" == "200" ]] || warn "el backend no responde 200 en $API/health (code=$code). Se continúa igualmente."

  log "Artefactos en: $OUT_DIR"
  snapshot preflight
}

# -----------------------------------------------------------------------------
# EJE 1 — Escalabilidad bajo carga
#
# Qué mide realmente:
#   - ramp de carga sostenida mientras el HPA reacciona
#   - para cada minuto: réplicas, CPU media, latencia p95, tasa de error
#   - el objetivo es dibujar la curva: réplicas vs carga
#
# El script NO decide el éxito por umbral fijo, sino que deja los datos para
# interpretarlos. Un umbral fijo de latencia depende demasiado de la máquina
# donde corre el test.
# -----------------------------------------------------------------------------
run_load_generator() {
  log "Levantando el generador de carga en $NS_OBS ($LOAD_PODS pods)…"

  kubectl -n "$NS_OBS" delete deployment kuber-loadgen --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl -n "$NS_OBS" delete configmap kuber-loadgen-script --ignore-not-found >/dev/null 2>&1 || true

  # El generador corre DENTRO del cluster a propósito: si lo ejecutamos desde
  # el host, la medición incluye el bottleneck de la red del portátil y los
  # números no dicen nada sobre el cluster.
  #
  # Aquí se publica SOLO el script (load.py). La configuración llega por
  # variables de entorno desde kuber-loadgen-config, que es el ConfigMap que el
  # Deployment referencia con configMapRef. Ponerla como --from-literal en este
  # ConfigMap no tenía efecto: este ConfigMap se monta como fichero, no como env.
  kubectl -n "$NS_OBS" create configmap kuber-loadgen-script \
    --from-file=load.py=./tests/load/load.py \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

  # La configuración del generador sí se sobreescribe aquí, y esta vez donde
  # importa: el ConfigMap que el pod lee de verdad.
  kubectl -n "$NS_OBS" create configmap kuber-loadgen-config \
    --from-literal=TARGET="$API" \
    --from-literal=RAMP_SECONDS="$RAMP_SECONDS" \
    --from-literal=MAX_RPS="${MAX_RPS:-500}" \
    --from-literal=HOLD_SECONDS="${HOLD_SECONDS:-120}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

  kubectl -n "$NS_OBS" apply -f ./tests/load/loadgen.yaml >/dev/null

  # ESTE era el bug: loadgen.yaml declara replicas: 0 a propósito, para que el
  # generador no esté gastando CPU fuera de las pruebas. Sin este scale, el
  # despliegue se quedaba en 0 réplicas y el test de estrés no generaba NINGÚN
  # tráfico: el HPA no se movía y el informe salía plano.
  kubectl -n "$NS_OBS" scale deploy kuber-loadgen --replicas="$LOAD_PODS" >/dev/null
  kubectl -n "$NS_OBS" rollout status deploy/kuber-loadgen --timeout=60s >/dev/null 2>&1 || true
  ok "generador desplegado y escalado a $LOAD_PODS réplica(s)"
}

collect_load_metrics() {
  local duration="$1" name="$2"
  local elapsed=0 interval=5

  log "Recolectando métricas durante ${duration}s…"
  : > "$OUT_DIR/${name}.csv"
  echo "ts,elapsed_s,hpa_current,hpa_desired,cpu_pods_mean,cpu_pods_max,mem_pods_mean,ready_pods,total_pods" \
    >> "$OUT_DIR/${name}.csv"

  while (( elapsed < duration )); do
    local hpa_cur hpa_des cpu_mean cpu_max mem_mean ready total
    hpa_cur="$(hpa_replicas)"
    hpa_des="$(kubectl -n "$NS_APP" get hpa "$BACKEND_HPA" -o jsonpath='{.spec.maxReplicas}' 2>/dev/null || echo '?')"
    # stats de metrics-server, agregadas por los pods del backend
    cpu_mean="$(kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods -n "$NS_APP" 2>/dev/null \
      | jq -r '[.items[] | select(.metadata.labels["app.kubernetes.io/name"]=="backend")
                | .containers[].usage.cpu | sub("m$";"") | tonumber] | (length>0) | if . then (add/length) else 0 end' 2>/dev/null || echo 0)"
    cpu_max="$(kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods -n "$NS_APP" 2>/dev/null \
      | jq -r '[.items[] | select(.metadata.labels["app.kubernetes.io/name"]=="backend")
                | .containers[].usage.cpu | sub("m$";"") | tonumber] | (length>0) | if . then max else 0 end' 2>/dev/null || echo 0)"
    mem_mean="$(kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods -n "$NS_APP" 2>/dev/null \
      | jq -r '[.items[] | select(.metadata.labels["app.kubernetes.io/name"]=="backend")
                | .containers[].usage.memory | sub("Ki$";"") | tonumber] | (length>0) | if . then (add/length|floor) else 0 end' 2>/dev/null || echo 0)"
    ready="$(kubectl -n "$NS_APP" get pods -l app.kubernetes.io/name=backend \
      --field-selector=status.phase=Running -o json 2>/dev/null \
      | jq -r '[.items[] | select([.status.conditions[]?|select(.type=="Ready")|.status]|.[0]=="True")] | length' 2>/dev/null || echo '?')"
    total="$(kubectl -n "$NS_APP" get pods -l app.kubernetes.io/name=backend --no-headers 2>/dev/null | wc -l | tr -d ' ')"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$(date +%H:%M:%S)" "$elapsed" "$hpa_cur" "$hpa_des" \
      "$cpu_mean" "$cpu_max" "$mem_mean" "$ready" "$total" >> "$OUT_DIR/${name}.csv"

    # Log legible cada 30s, no en cada sample: el CSV es el dato fino
    (( elapsed % 30 == 0 )) && log "  t=${elapsed}s  pods=${ready}/${total}  hpa=${hpa_cur}  cpu_mean=${cpu_mean}m  cpu_max=${cpu_max}m  mem=${mem_mean}Ki"

    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  ok "métricas guardadas en $OUT_DIR/${name}.csv"
}

stress_test() {
  log "=== EJE 1: ESCALABILIDAD ==="
  local dur=$((RAMP_SECONDS + 120))
  run_load_generator
  collect_load_metrics "$dur" "stress"
  log "Escalado observado:"
  awk -F, 'NR>1 {print $1"  hpa="$3"  pods_ready="$8"/"$9}' "$OUT_DIR/stress.csv" | tail -20

  # Limpieza: devolver el generador a cero y dejar el HPA recolocar
  kubectl -n "$NS_OBS" scale deploy kuber-loadgen --replicas=0 >/dev/null 2>&1 || true
  snapshot post-stress
}

# -----------------------------------------------------------------------------
# EJE 2 — Tolerancia a fallos
#
# Cada escenario mide la misma cosa: cuántos requests se pierden realmente.
# La diferencia entre "el pod se reinició" y "el usuario recibió un error" es
# exactamente lo que mide el tráfico continuo durante el fallo.
# -----------------------------------------------------------------------------
# Detector de peticiones fallidas en tiempo real.
# Corre requests rápidas y cuenta 5xx / timeouts durante N segundos.
watch_errors() {
  local duration="$1" out_file="$2"
  local deadline=$(( $(date +%s) + duration ))
  local total=0 fails=0
  : > "$out_file"
  echo "ts,code" >> "$out_file"
  while (( $(date +%s) < deadline )); do
    # 5 requests por ciclo, en paralelo: más presión sobre el sistema que un
    # loop secuencial, que se quedaría esperando la latencia en cada iteración.
    local results
    results="$(for _ in 1 2 3 4 5; do
      curl -s -o /dev/null -w '%{http_code} ' --max-time 3 "$API/whoami" 2>/dev/null || echo -n "000 "
    done)"
    for code in $results; do
      echo "$(date +%H:%M:%S),$code" >> "$out_file"
      total=$((total+1))
      [[ "$code" =~ ^5|^0 ]] && fails=$((fails+1))
    done
    sleep 0.2
  done
  echo "$total $fails"
}

report_errors() {
  local out_file="$1" label="$2"
  local total fails pct
  # NR>1 para saltar la cabecera "ts,code". Contarla con grep -c inflaba el
  # total en 1 y subestimaba el porcentaje de fallo.
  total="$(awk 'NR>1' "$out_file" | wc -l | tr -d ' ')"
  fails="$(awk -F, 'NR>1 && ($2 ~ /^5/ || $2 ~ /^0/)' "$out_file" | wc -l | tr -d ' ')"
  if [[ "$total" -eq 0 ]]; then
    err "$label: sin muestras (¿el target no responde?)"
    return
  fi
  pct="$(awk -v f="$fails" -v t="$total" 'BEGIN{printf "%.2f", f*100/t}')"
  if [[ "$fails" -eq 0 ]]; then
    ok "$label: 0 fallos de $total requests (0.00%)"
  else
    warn "$label: $fails fallos de $total requests (${pct}%)"
  fi
  echo "  $label,$total,$fails,$pct" >> "$OUT_DIR/resumen.csv"
}

chaos_test() {
  log "=== EJE 2: TOLERANCIA A FALLOS ==="

  # ---------------------------------------------------------------------------
  # 2.1 Matar un pod del backend con tráfico en curso
  #     Expectativa: 0 (o unas pocas) peticiones perdidas. El Service retira el
  #     endpoint antes de que el pod muera, y traefik reintenta.
  # ---------------------------------------------------------------------------
  local victim
  victim="$(kubectl -n "$NS_APP" get pods -l app.kubernetes.io/name=backend -o jsonpath='{.items[0].metadata.name}')"
  if [[ -n "$victim" ]]; then
    log "2.1 Eliminando el pod $victim con tráfico en curso…"
    watch_errors 30 "$OUT_DIR/chaos-podkill.csv" > /tmp/_pe.$$ &
    local pid=$!
    sleep 5   # dejar que el tráfico se estabilice antes de romper nada
    kubectl -n "$NS_APP" delete pod "$victim" --grace-period=0 --force --wait=false >/dev/null 2>&1 || true
    sleep 8   # margen para que el scheduler reemplace el pod
    wait "$pid"
    report_errors "$OUT_DIR/chaos-podkill.csv" "pod-kill($victim)"

    log "  esperando reemplazo del pod…"
    kubectl -n "$NS_APP" rollout status deploy/"$BACKEND_DEPLOY" --timeout=180s >/dev/null 2>&1 || true
    snapshot post-podkill
  else
    warn "2.1 omitido: no hay pods del backend"
  fi

  # ---------------------------------------------------------------------------
  # 2.2 Reiniciar la base de datos
  #     Este es el escenario más duro y el más informativo. Lo que hay que
  #     observar NO es que las peticiones fallen (es inevitable), sino:
  #       a) el backend NO se reinicia en cascada  (readiness, no liveness)
  #       b) los reinicios del contenedor se quedan en 0 o 1
  #       c) cuando la BD vuelve, el servicio se recupera solo
  # ---------------------------------------------------------------------------
  log "2.2 Reiniciando PostgreSQL con tráfico en curso…"
  local restarts_before restarts_after
  restarts_before="$(kubectl -n "$NS_APP" get pods -l app.kubernetes.io/name=backend \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | paste -sd+ - | bc 2>/dev/null || echo 0)"

  watch_errors 60 "$OUT_DIR/chaos-pgkill.csv" > /tmp/_pg.$$ &
  local pid=$!
  sleep 5
  kubectl -n "$NS_DATA" delete pod -l app.kubernetes.io/name=postgres --wait=false >/dev/null 2>&1 || true
  # Postgres tarda en volver: 60s de recovery + arranque
  log "  esperando a que PostgreSQL vuelva a estar ready…"
  local pg_ok=false
  for _ in $(seq 1 40); do
    if [[ "$(probe "$API/health" 3 | cut -d' ' -f1)" == "200" ]]; then pg_ok=true; break; fi
    sleep 5
  done
  wait "$pid" || true

  restarts_after="$(kubectl -n "$NS_APP" get pods -l app.kubernetes.io/name=backend \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | paste -sd+ - | bc 2>/dev/null || echo 0)"

  report_errors "$OUT_DIR/chaos-pgkill.csv" "postgres-restart"

  if [[ "$pg_ok" == "true" ]]; then
    ok "el servicio se recuperó solo tras la caída de la BD (RPO de Availability: sin intervención)"
  else
    err "el servicio NO se recuperó solo tras la caída de la BD"
  fi

  # El test clave de la arquitectura de sondas
  if [[ "${restarts_after:-0}" -le $(( ${restarts_before:-0} + 1 )) ]]; then
    ok "no hubo reinicio en cascada del backend (restarts: $restarts_before -> $restarts_after)"
  else
    err "el backend se reinició en cascada al caer la BD (restarts: $restarts_before -> $restarts_after)"
    warn "  esto indica que liveness está comprobando la BD. debe comprobar solo el proceso."
  fi
  echo "  postgres-restart,restartos,$restarts_before,$restarts_after" >> "$OUT_DIR/resumen.csv"
  snapshot post-pgkill

  # ---------------------------------------------------------------------------
  # 2.3 Matar un nodo entero
  #     Verifica el PDB, el rescheduling y el topología. kind no simula bien la
  #     caída de hardware de un nodo (los contenedores siguen vivos), así que
  #     esto se hace con drain + cordon, que es el equivalente operacional.
  # ---------------------------------------------------------------------------
  log "2.3 Simulando la caída de un nodo (cordon + drain)…"
  local node
  node="$(kubectl get nodes -l workload=app -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "$node" ]]; then
    watch_errors 45 "$OUT_DIR/chaos-nodedrain.csv" > /tmp/_nd.$$ &
    local pid=$!
    sleep 5
    kubectl cordon "$node" >/dev/null 2>&1 || true
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --force --timeout=180s >/dev/null 2>&1 || true
    wait "$pid" || true
    report_errors "$OUT_DIR/chaos-nodedrain.csv" "node-drain($node)"

    log "  restaurando el nodo…"
    kubectl uncordon "$node" >/dev/null 2>&1 || true
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=180s >/dev/null 2>&1 || true
  else
    warn "2.3 omitido: no hay nodos etiquetados workload=app"
  fi
  snapshot post-nodedrain

  # ---------------------------------------------------------------------------
  # 2.4 Drenar el frontend con tráfico en curso
  # ---------------------------------------------------------------------------
  log "2.4 Drenando el frontend (verifica el PDB)…"
  watch_errors 30 "$OUT_DIR/chaos-frontenddrain.csv" > /tmp/_fd.$$ &
  local pid=$!
  sleep 5
  kubectl -n "$NS_APP" rollout restart deploy/kuber-frontend >/dev/null 2>&1 || true
  wait "$pid" || true
  report_errors "$OUT_DIR/chaos-frontenddrain.csv" "frontend-rollout"
  snapshot post-frontenddrain
}

# -----------------------------------------------------------------------------
# EJE 3 — Endurance (soak)
#
# Carga moderada durante mucho tiempo. Busca fallos que solo aparecen con el
# tiempo: fugas de memoria (que disparan el HPA de memoria sin parar),
# conexiones que se agotan, y Deriva del pool de Postgres.
# -----------------------------------------------------------------------------
soak_test() {
  log "=== EJE 3: ENDURANCE (${SOAK_MINUTES} min a carga moderada) ==="
  kubectl -n "$NS_OBS" scale deploy kuber-loadgen --replicas="$LOAD_PODS" >/dev/null 2>&1 || true
  local dur=$((SOAK_MINUTES * 60))
  collect_load_metrics "$dur" "soak"
  kubectl -n "$NS_OBS" scale deploy kuber-loadgen --replicas=0 >/dev/null 2>&1 || true
  snapshot post-soak
}

# -----------------------------------------------------------------------------
# Informe
# -----------------------------------------------------------------------------
summary() {
  log "=== RESUMEN ==="
  echo "run: $STAMP" | tee "$OUT_DIR/resumen.txt"
  echo "target: $TARGET" | tee -a "$OUT_DIR/resumen.txt"
  echo | tee -a "$OUT_DIR/resumen.txt"
  if [[ -f "$OUT_DIR/resumen.csv" ]]; then
    column -t -s, "$OUT_DIR/resumen.csv" 2>/dev/null | tee -a "$OUT_DIR/resumen.txt" \
      || cat "$OUT_DIR/resumen.csv" | tee -a "$OUT_DIR/resumen.txt"
  fi
  echo | tee -a "$OUT_DIR/resumen.txt"
  echo "artefactos: $OUT_DIR" | tee -a "$OUT_DIR/resumen.txt"
  log "Curvas: $OUT_DIR/stress.csv  $OUT_DIR/soak.csv"
  log "Errores: $OUT_DIR/chaos-*.csv"
  log "Estado del cluster: $OUT_DIR/cluster-*.log"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  local mode="${1:-all}"
  mkdir -p "$OUT_DIR"
  echo "scenario,total,failed,pct" > "$OUT_DIR/resumen.csv"

  case "$mode" in
    stress) preflight; stress_test; summary ;;
    chaos)  preflight; chaos_test;  summary ;;
    soak)   preflight; soak_test;   summary ;;
    all)
      preflight
      log ""
      warn "El modo 'all' encadena los tres ejes. El eje de caos deja el cluster"
      warn "en un estado degradado a propósito durante unos segundos. No lo uses"
      warn "contra nada que esté en producción real."
      log ""
      stress_test
      log ""
      chaos_test
      log ""
      # el soak por defecto es largo; se puede saltar con SOAK_MINUTES=0
      if [[ "$SOAK_MINUTES" -gt 0 ]]; then soak_test; else warn "soak omitido (SOAK_MINUTES=0)"; fi
      summary
      ;;
    *)
      die "uso: $0 {all|stress|chaos|soak}"
      ;;
  esac
}

main "$@"
