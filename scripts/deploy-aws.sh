#!/usr/bin/env bash
# =============================================================================
# Despliega (o actualiza) la aplicación en el clúster EKS. Idempotente: lo usa
# tanto el pipeline de CD como una persona desde su terminal.
#
# Requiere: kubectl apuntando al clúster, helm, python3, openssl, y las
# imágenes ya publicadas en ECR.
#
#   IMAGE_TAG=abc1234 ECR_REGISTRY=<cuenta>.dkr.ecr.us-east-1.amazonaws.com \
#     ./scripts/deploy-aws.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?falta IMAGE_TAG (etiqueta de las imágenes en ECR)}"
: "${ECR_REGISTRY:?falta ECR_REGISTRY (cuenta.dkr.ecr.region.amazonaws.com)}"
ECR_PROJECT="${ECR_PROJECT:-chaos-lab}"
METRICS_SERVER_VERSION="${METRICS_SERVER_VERSION:-v0.7.2}"
TRAEFIK_CHART_VERSION="${TRAEFIK_CHART_VERSION:-33.2.1}"

log() { echo "==> $*"; }
rand() { openssl rand -hex 16; }

# Reintenta un comando con la red lenta (pulls de imagen / repos Helm).
# Uso: retry 3 "descripcion" comando -- args...
retry() {
  local attempts="$1"; shift
  local desc="$1"; shift
  local i
  for i in $(seq 1 "$attempts"); do
    if "$@"; then return 0; fi
    echo "   ... $desc fallo (intento $i/$attempts), reintentando en 10s"
    sleep 10
  done
  echo "   !! $desc fallo tras $attempts intentos"
  return 1
}

# --- 1. Namespaces -----------------------------------------------------------
log "namespaces"
kubectl apply -f cluster/base/00-namespace.yaml

# --- 2. metrics-server (lo necesita el HPA) -----------------------------------
log "metrics-server $METRICS_SERVER_VERSION"
retry 3 "descargar metrics-server" kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"

# --- 3. Traefik (Ingress + CRDs de los Middleware) ------------------------------
log "traefik $TRAEFIK_CHART_VERSION"
helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
retry 3 "actualizar repo helm traefik" helm repo update >/dev/null
helm upgrade --install traefik traefik/traefik \
  --version "$TRAEFIK_CHART_VERSION" \
  --namespace traefik --create-namespace \
  --set service.type=LoadBalancer \
  --set deployment.replicas=2 \
  --set 'nodeSelector.workload=app' \
  --wait --timeout 900s

# --- 4. Secretos: se crean UNA vez con valores aleatorios (nada en git) --------
log "secretos"
# Postgres vive en EC2: su contraseña viene de Terraform (random_password).
# Orden de prioridad: env vars del CD > terraform output local > error.
# Si no hay credenciales, fallar antes que inventar una password random
# (que nunca coincidiría con el EC2): ese fallback anterior dejaba el
# backend sin conectar. En GitHub hay que pasar PG_PASS/PG_HOST como secrets.
PG_PASS="${PG_PASS:-}"
PG_HOST="${PG_HOST:-}"
if [ -z "$PG_PASS" ] && [ -d infra/terraform ]; then
  PG_PASS="$(terraform -chdir=infra/terraform output -raw postgres_password 2>/dev/null || true)"
fi
if [ -z "$PG_HOST" ] && [ -d infra/terraform ]; then
  PG_HOST="$(terraform -chdir=infra/terraform output -raw postgres_private_ip 2>/dev/null || true)"
fi
if [ -z "$PG_PASS" ] || [ -z "$PG_HOST" ]; then
  echo "ERROR: PG_PASS/PG_HOST no encontrados (ni env, ni terraform output)."
  echo "       En CD: declaralos como secrets PG_PASS y PG_HOST."
  echo "       En local: terraform apply primero y reintentá."
  exit 1
fi
log "Postgres en EC2: ${PG_HOST}"
if ! kubectl -n kuber-data get secret kuber-postgres-secret >/dev/null 2>&1; then
  kubectl -n kuber-data create secret generic kuber-postgres-secret \
    --from-literal=POSTGRES_PASSWORD="$PG_PASS" \
    --from-literal=POSTGRES_SUPERUSER_PASSWORD="$PG_PASS"
fi
if ! kubectl -n kuber-app get secret kuber-backend-secret >/dev/null 2>&1; then
  kubectl -n kuber-app create secret generic kuber-backend-secret \
    --from-literal=DB_PASSWORD="$PG_PASS" \
    --from-literal=ADMIN_TOKEN="$(rand)"
fi

# --- 5. Semillas (ConfigMap grande: create, no apply) ---------------------------
if ! kubectl -n kuber-app get configmap kuber-seed-data >/dev/null 2>&1; then
  log "semillas"
  python3 app/backend/seed.py --out seeds --seed 1337 --events 40 --hot 5 --users 300 >/dev/null
  kubectl -n kuber-app create configmap kuber-seed-data --from-file=seeds/
fi

# --- 6. Aplicación: overlay AWS con las imágenes de ECR -------------------------
log "aplicación (imágenes ${ECR_REGISTRY}/${ECR_PROJECT}/*:${IMAGE_TAG}, DB_HOST=${PG_HOST})"
kubectl kustomize cluster/overlays/aws \
  | sed -e "s#image: kuber/backend:1.0.0#image: ${ECR_REGISTRY}/${ECR_PROJECT}/backend:${IMAGE_TAG}#" \
        -e "s#image: kuber/frontend:1.0.0#image: ${ECR_REGISTRY}/${ECR_PROJECT}/frontend:${IMAGE_TAG}#" \
        -e "s#value: 127.0.0.1#value: ${PG_HOST}#g" \
  | kubectl apply -f -
log "esperando el despliegue (timeouts largos por red lenta / pull de imagenes)"
kubectl -n kuber-app rollout status deploy/kuber-backend  --timeout=900s
kubectl -n kuber-app rollout status deploy/kuber-frontend --timeout=600s

# --- 7. Monitorización (Prometheus + Grafana) -----------------------------------
# NO bloqueante: es el componente mas pesado y con mas imagenes que bajar.
# Con red lenta puede demorar mucho; la app ya quedo desplegada (paso 6).
# Si falla, se avisa pero el CD NO se marca como fallido (ni dispara rollback).
log "monitorización (opcional, no bloquea el despliegue)"
if {
  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
  if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
    kubectl -n monitoring create secret generic grafana-admin \
      --from-literal=admin-user=admin --from-literal=admin-password="$(rand)"
  fi
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  retry 3 "actualizar repo helm prometheus" helm repo update >/dev/null
  helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
    ${KPS_VERSION:+--version "$KPS_VERSION"} \
    --namespace monitoring -f monitoring/values.yaml --wait --timeout 900s
  kubectl apply -f monitoring/servicemonitor.yaml -f monitoring/dashboard.yaml
}; then
  log "monitorización instalada"
else
  echo "WARN: monitorización no se instalo (red lenta o timeout). La app sigue arriba."
  echo "      Reintentalo a mano: helm upgrade --install monitoring prometheus-community/kube-prometheus-stack -n monitoring -f monitoring/values.yaml"
fi

# --- 8. URL pública ---------------------------------------------------------------
log "esperando el balanceador (hasta 10 min: el DNS de AWS tarda en propagarse)"
LB=""
for _ in $(seq 1 120); do
  LB="$(kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [ -n "$LB" ] && break
  sleep 5
done
echo
echo "Aplicación:  http://${LB:-<pendiente>}"
echo "Grafana:     kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80"
echo "  usuario admin; contraseña: kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
