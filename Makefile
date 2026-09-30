# =============================================================================
# Makefile — atajos para el ciclo completo
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

REGISTRY ?= kuber
TAG      ?= 1.0.0
KIND     ?= kuber
KUBECTL  ?= kubectl

# Versiones fijadas: reproducir el entorno de pruebas exige las mismas tools
KIND_VERSION     ?= v0.24.0
K8S_VERSION      ?= v1.31.2
METRICS_SERVER_VERSION ?= v0.7.2
POSTGRES_IMAGE   ?= postgres:16.4-alpine

.PHONY: help
help: ## Muestra esta ayuda
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Requisitos
# -----------------------------------------------------------------------------
.PHONY: check
check: ## Verifica que están las dependencias del entorno
	@for c in kubectl kind docker jq curl; do \
	  command -v $$c >/dev/null 2>&1 && echo "  OK      $$c" || echo "  FALTA   $$c"; \
	done
	@command -v docker >/dev/null 2>&1 && \
	  docker info >/dev/null 2>&1 && echo "  OK      docker daemon" || \
	  echo "  ATENCIÓN  el daemon de Docker no responde (¿lo arrancaste?)"

# -----------------------------------------------------------------------------
# Cluster
# -----------------------------------------------------------------------------
.PHONY: cluster-up
cluster-up: check ## Crea el cluster de 4 nodos con kind
	kind create cluster --config cluster/kind-config.yaml --wait 120s
	$(MAKE) metrics-server
	$(MAKE) images-load
	$(MAKE) deploy

.PHONY: cluster-down
cluster-down: ## Borra el cluster y sus volúmenes
	kind delete cluster --name $(KIND)

.PHONY: cluster-recreate
cluster-recreate: cluster-down cluster-up ## Borra y vuelve a crear desde cero

.PHONY: metrics-server
metrics-server: ## Instala metrics-server (necesario para el HPA de CPU/memoria)
	@echo "==> metrics-server $(METRICS_SERVER_VERSION)"
	@kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/$(METRICS_SERVER_VERSION)/components.yaml
	@kubectl -n kube-system patch deployment metrics-server --type=json \
	  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
	@echo "    (--kubelet-insecure-tls es necesario en kind: los certificados del"
	@echo "     kubelet son autofirmados y metrics-server los rechaza sin él)"

# -----------------------------------------------------------------------------
# Imágenes
# -----------------------------------------------------------------------------
.PHONY: images-build
images-build: ## Construye las imágenes de backend, frontend y loadgen
	docker build -t $(REGISTRY)/backend:$(TAG)  -f app/backend/Dockerfile  app/backend
	docker build -t $(REGISTRY)/frontend:$(TAG) -f app/frontend/Dockerfile app/frontend
	docker build -t $(REGISTRY)/loadgen:$(TAG)  -f tests/load/Dockerfile  tests/load

.PHONY: images-load
images-load: ## Carga las imágenes construidas al cluster, sin registry
	@for img in backend frontend loadgen; do \
	  echo "==> $(REGISTRY)/$$img:$(TAG)"; \
	  kind load docker-image $(REGISTRY)/$$img:$(TAG) --name $(KIND); \
	done
	@echo "    (kind load evita tener que montar un registry para las pruebas)"

.PHONY: images-push
images-push: ## Sube las imágenes al registry (requiere REGISTRY real)
	docker push $(REGISTRY)/backend:$(TAG)
	docker push $(REGISTRY)/frontend:$(TAG)
	docker push $(REGISTRY)/loadgen:$(TAG)

# -----------------------------------------------------------------------------
# Despliegue
# -----------------------------------------------------------------------------
.PHONY: deploy
deploy: ## Aplica todos los manifiestos de cluster/base
	@echo "==> aplicando manifiestos"
	@$(KUBECTL) apply -f cluster/base/00-namespace.yaml
	@# La BD primero: el backend no arranca sin ella
	@$(KUBECTL) -n kuber-data rollout status statefulset/kuber-postgres --timeout=180s || true
	@$(KUBECTL) apply -f cluster/base/01-configmap.yaml
	@$(KUBECTL) apply -f cluster/base/02-secret.yaml
	@$(KUBECTL) apply -f cluster/base/03-postgres.yaml
	@$(KUBECTL) apply -f cluster/base/04-backend.yaml
	@# Las semillas se publican antes que el frontend
	@$(MAKE) seeds-data
	@$(KUBECTL) apply -f cluster/base/05-frontend.yaml
	@$(KUBECTL) apply -f cluster/base/06-ingress.yaml
	@$(MAKE) loadgen-config
	@echo "==> esperando a que todo esté ready"
	@$(KUBECTL) -n kuber-app  rollout status deploy/kuber-backend  --timeout=300s
	@$(KUBECTL) -n kuber-app  rollout status deploy/kuber-frontend --timeout=180s
	@$(KUBECTL) -n kuber-data rollout status statefulset/kuber-postgres --timeout=180s
	@$(MAKE) status

.PHONY: undeploy
undeploy: ## Borra los manifiestos (conserva los volúmenes)
	-$(KUBECTL) delete -f cluster/base/06-ingress.yaml --ignore-not-found
	-$(KUBECTL) delete -f cluster/base/05-frontend.yaml --ignore-not-found
	-$(KUBECTL) delete -f cluster/base/04-backend.yaml --ignore-not-found
	-$(KUBECTL) delete -f cluster/base/03-postgres.yaml --ignore-not-found
	-$(KUBECTL) delete -f cluster/base/01-configmap.yaml --ignore-not-found
	-$(KUBECTL) delete -f cluster/base/02-secret.yaml --ignore-not-found
	-$(KUBECTL) delete namespace kuber-app kuber-data kuber-observe --ignore-not-found

.PHONY: destroy
destroy: ## Borra todo, incluidos los volúmenes persistentes
	-$(KUBECTL) delete namespace kuber-app kuber-data kuber-observe --ignore-not-found

.PHONY: loadgen-config
loadgen-config: ## Publica el script del generador como ConfigMap
	@$(KUBECTL) -n kuber-observe create configmap kuber-loadgen-script \
	  --from-file=load.py=./tests/load/load.py \
	  --dry-run=client -o yaml | $(KUBECTL) apply -f -

# -----------------------------------------------------------------------------
# Semillas
# -----------------------------------------------------------------------------
# El generador es determinista: la misma semilla da exactamente el mismo
# dataset. Eso es lo que hace comparables dos ejecuciones de una prueba.
SEED ?= 1337
SEED_EVENTS ?= 40
SEED_HOT ?= 5
SEED_USERS ?= 300

.PHONY: seeds
seeds: ## Genera las semillas sintéticas en seeds/ (SEED=1337 make seeds)
	@python3 app/backend/seed.py \
	  --out seeds \
	  --seed $(SEED) \
	  --events $(SEED_EVENTS) \
	  --hot $(SEED_HOT) \
	  --users $(SEED_USERS)

.PHONY: seeds-check
seeds-check: ## Verifica que el generador es determinista
	@python3 app/backend/seed.py --out /tmp/kuber-seeds-a --seed $(SEED) \
	  --events $(SEED_EVENTS) --hot $(SEED_HOT) --users $(SEED_USERS) >/dev/null
	@python3 app/backend/seed.py --out /tmp/kuber-seeds-b --seed $(SEED) \
	  --events $(SEED_EVENTS) --hot $(SEED_HOT) --users $(SEED_USERS) >/dev/null
	@if diff -r /tmp/kuber-seeds-a /tmp/kuber-seeds-b >/dev/null; then \
	  echo "OK: el dataset es reproducible con la semilla $(SEED)"; \
	else \
	  echo "FALLO: dos ejecuciones con la misma semilla difieren"; exit 1; \
	fi
	@rm -rf /tmp/kuber-seeds-a /tmp/kuber-seeds-b

.PHONY: seeds-data
seeds-data: seeds ## Genera las semillas y las publica como ConfigMap
	@$(KUBECTL) -n kuber-app create configmap kuber-seed-data \
	  --from-file=seeds/ \
	  --dry-run=client -o yaml | $(KUBECTL) apply -f -
	@echo "semillas publicadas. reinicia el backend con: make restart-backend"

.PHONY: restart-backend
restart-backend: ## Reinicia el backend para que recargue las semillas
	@$(KUBECTL) -n kuber-app rollout restart deploy/kuber-backend
	@$(KUBECTL) -n kuber-app rollout status deploy/kuber-backend --timeout=180s

# -----------------------------------------------------------------------------
# Inspección
# -----------------------------------------------------------------------------
.PHONY: status
status: ## Estado de deployments, HPA, pods y endpoints
	@echo "==> deployments"
	@$(KUBECTL) get deploy,statefulset -A -o wide 2>/dev/null || true
	@echo
	@echo "==> HPA"
	@$(KUBECTL) get hpa -A 2>/dev/null || true
	@echo
	@echo "==> pods"
	@$(KUBECTL) get pods -A -o wide 2>/dev/null || true
	@echo
	@echo "==> endpoints"
	@$(KUBECTL) get endpoints -A 2>/dev/null || true
	@echo
	@echo "URL: http://localhost:8080  (frontend)"
	@echo "     http://localhost:8080/api/v1/whoami  (backend)"

.PHONY: logs
logs: ## Sigue los logs del backend
	$(KUBECTL) -n kuber-app logs -f deploy/kuber-backend --tail=100 --prefix

.PHONY: logs-all
logs-all: ## Logs de todo el namespace de la app
	$(KUBECTL) -n kuber-app logs -l app.kubernetes.io/name=backend --all-containers --tail=50 --prefix

.PHONY: metrics
metrics: ## Muestra el uso de CPU y memoria actual
	@$(KUBECTL) top pods -n kuber-app 2>/dev/null || \
	  echo "metrics-server no disponible. ejecuta: make metrics-server"

.PHONY: describe-hpa
describe-hpa: ## Detalle del HPA del backend
	$(KUBECTL) -n kuber-app describe hpa kuber-backend-hpa

.PHONY: scale
scale: ## Fija las réplicas del backend (make scale REPLICAS=5)
	$(KUBECTL) -n kuber-app scale deploy/kuber-backend --replicas=$(REPLICAS)

.PHONY: port-forward
port-forward: ## Reenvía el backend a localhost:9000
	$(KUBECTL) -n kuber-app port-forward svc/kuber-backend 9000:8080

# -----------------------------------------------------------------------------
# Pruebas
# -----------------------------------------------------------------------------
.PHONY: test-stress
test-stress: ## Eje 1 — escalabilidad bajo carga
	./scripts/autopilot.sh stress

.PHONY: test-chaos
test-chaos: ## Eje 2 — tolerancia a fallos
	./scripts/autopilot.sh chaos

.PHONY: test-soak
test-soak: ## Eje 3 — endurance (SOAK_MINUTES=60 make test-soak)
	./scripts/autopilot.sh soak

.PHONY: test-all
test-all: ## Los tres ejes + informe
	./scripts/autopilot.sh all

.PHONY: validate
validate: ## Valida los manifiestos contra el esquema real del API server
	@# OJO: --dry-run=client NO valida el esquema, solo el formato. Los campos
	@# inventados (un LimitRange con spec.requests, un Middleware con timeouts)
	@# pasan ese chequeo y los rechaza el API server al aplicar. Por eso aquí se
	@# usa dry-run=server, que sí consulta el OpenAPI del cluster.
	@kubectl cluster-info >/dev/null 2>&1 || { \
	  echo "ERROR: no hay cluster accesible. Levanta uno con: make cluster-up"; exit 1; }
	@for f in cluster/kind-config.yaml cluster/base/*.yaml tests/load/loadgen.yaml; do \
	  case "$$f" in *kind-config.yaml) continue;; esac; \
	  echo "==> $$f"; \
	  $(KUBECTL) apply --dry-run=server -f "$$f" >/dev/null || exit 1; \
	done
	@echo "todos los manifiestos son válidos contra el esquema del API server"
	@echo
	@echo "Nota: el Middleware de Traefik es un CRD. Se valida en el servidor, pero"
	@echo "solo si el CRD está instalado. Si no lo está, kind lo trae de serie."

# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------
.PHONY: psql
psql: ## Abre una sesión psql contra Postgres
	$(KUBECTL) -n kuber-data exec statefulset/kuber-postgres -- \
	  psql -U kuber -d kuber

.PHONY: clean
clean: ## Borra los informes de pruebas
	rm -rf reports
