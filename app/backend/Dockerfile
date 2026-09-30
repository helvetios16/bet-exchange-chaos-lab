# =============================================================================
# Dockerfile del backend — multi-stage, non-root, sin shell en la imagen final
# =============================================================================
# --- Stage 1: deps ---
FROM python:3.12-slim AS deps

# No instalar Recommended de apt: reduce superficie y tamano de imagen
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
# Build wheels en esta stage para que la final no arrastre toolchain
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: runtime ---
FROM python:3.12-slim AS runtime

# curl para la sonda si se cambiara a exec; y ca-certificates
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 kuber \
 && useradd -u 10001 -g 10001 -m -s /sbin/nologin kuber

COPY --from=deps /install /usr/local

WORKDIR /app
COPY --chown=10001:10001 app.py seed.py seed_load.py schema.sql ./

# --- Semillas ---
# Las semillas NO se hornean en la imagen. Van en un ConfigMap montado en
# /app/seeds, para que regenerarlas (make seeds) no obligue a reconstruir la
# imagen. El esquema sí va en la imagen: es código, no datos.
RUN mkdir -p /app/seeds && chown 10001:10001 /app/seeds

USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    SCHEMA_PATH=/app/schema.sql \
    SEED_DIR=/app/seeds \
    SEED_ON_START=true

EXPOSE 8080

# HEALTHCHECK del Dockerfile: se ignora en Kubernetes (ahi mandan las
# startup/readiness/liveness probes), pero sirve para docker run local.
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8080/health/live || exit 1

# exec form + uvicorn como entrypoint: PID 1 es uvicorn y recibe SIGTERM
# directamente, que es lo que dispara el graceful shutdown.
#
# No se usa `python -m app` a proposito: ese path ejecuta el bloque
# __main__, que a su vez llama a uvicorn.run("app:app") y re-importa el
# modulo, creando un segundo pool de conexiones en el mismo proceso.
CMD ["uvicorn", "app:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--no-access-log", \
     "--timeout-keep-alive", "20", \
     "--log-level", "info"]
