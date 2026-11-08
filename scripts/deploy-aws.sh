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

# --- 1. Namespaces -----------------------------------------------------------
log "namespaces"
kubectl apply -f cluster/base/00-namespace.yaml

# --- 2. metrics-server (lo necesita el HPA) -----------------------------------
log "metrics-server $METRICS_SERVER_VERSION"
kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"

# --- 3. Traefik (Ingress + CRDs de los Middleware) ------------------------------
log "traefik $TRAEFIK_CHART_VERSION"
helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install traefik traefik/traefik \
  --version "$TRAEFIK_CHART_VERSION" \
  --namespace traefik --create-namespace \
  --set service.type=LoadBalancer \
  --set deployment.replicas=2 \
  --set 'nodeSelector.workload=app' \
  --wait --timeout 300s

# --- 4. Secretos: se crean UNA vez con valores aleatorios (nada en git) --------
log "secretos"
if ! kubectl -n kuber-data get secret kuber-postgres-secret >/dev/null 2>&1; then
  PG_PASS="$(rand)"
  kubectl -n kuber-data create secret generic kuber-postgres-secret \
    --from-literal=POSTGRES_PASSWORD="$PG_PASS" \
    --from-literal=POSTGRES_SUPERUSER_PASSWORD="$PG_PASS"
fi
if ! kubectl -n kuber-app get secret kuber-backend-secret >/dev/null 2>&1; then
  PG_PASS="$(kubectl -n kuber-data get secret kuber-postgres-secret \
    -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)"
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
log "aplicación (imágenes ${ECR_REGISTRY}/${ECR_PROJECT}/*:${IMAGE_TAG})"
kubectl kustomize cluster/overlays/aws \
  | sed -e "s#image: kuber/backend:1.0.0#image: ${ECR_REGISTRY}/${ECR_PROJECT}/backend:${IMAGE_TAG}#" \
        -e "s#image: kuber/frontend:1.0.0#image: ${ECR_REGISTRY}/${ECR_PROJECT}/frontend:${IMAGE_TAG}#" \
  | kubectl apply -f -

log "esperando el despliegue"
kubectl -n kuber-data rollout status statefulset/kuber-postgres --timeout=300s
kubectl -n kuber-app rollout status deploy/kuber-backend  --timeout=300s
kubectl -n kuber-app rollout status deploy/kuber-frontend --timeout=180s

# --- 7. Monitorización (Prometheus + Grafana) -----------------------------------
log "monitorización"
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$(rand)"
fi
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  ${KPS_VERSION:+--version "$KPS_VERSION"} \
  --namespace monitoring -f monitoring/values.yaml --wait --timeout 600s
kubectl apply -f monitoring/servicemonitor.yaml -f monitoring/dashboard.yaml

# --- 8. URL pública ---------------------------------------------------------------
log "esperando el balanceador"
LB=""
for _ in $(seq 1 60); do
  LB="$(kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [ -n "$LB" ] && break
  sleep 5
done
echo
echo "Aplicación:  http://${LB:-<pendiente>}"
echo "Grafana:     kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80"
echo "  usuario admin; contraseña: kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
