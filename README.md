# Proyecto Integrador de Cloud Computing — Guía del equipo

> **Si acabas de llegar al repo, empieza aquí.** Esta sección explica qué hicimos
> sobre la aplicación original, cómo está organizado y cómo probarlo. Debajo
> (a partir de «Documentación original del laboratorio») está el README de la
> aplicación base, que sigue siendo válido para entender el dominio.

> **¿Quieres hacerlo funcionar?** Sigue el paso a paso detallado en
> [`docs/TUTORIAL-EQUIPO.md`](docs/TUTORIAL-EQUIPO.md) (instalación, tests,
> AWS, despliegue, evidencias y problemas frecuentes).

## 1. ¿De qué va?

La aplicación base es un **simulador de un exchange de apuestas** (FastAPI +
nginx + PostgreSQL, con datos inventados) que ya corría en Kubernetes local con
`kind`. Para el curso de **Cloud Computing** la llevamos a un ciclo completo
cloud-native en **AWS**:

```
Código ──► PR en GitHub ──► CI (lint + tests + build)
                               │  verde en main
                               ▼
                       CD ──► Amazon ECR (imagen:SHA) ──► Amazon EKS
                                                             │
        Usuario ──► Balanceador AWS ──► Traefik ──► backend ×2..8 (HPA) ──► PostgreSQL
                                              └──► frontend ×2..6 (HPA)

        Prometheus + Grafana ◄── métricas de la app, pods, CPU y memoria
        Terraform crea y destruye la infraestructura (EKS, nodos, ECR)
```

La propiedad que se vigila en todas las pruebas es **`drift = 0`**: el saldo de
`wallets.available` debe coincidir siempre con la suma del ledger. Si una
apuesta, una liquidación o un fallo pierde o inventa dinero, `drift` deja de
ser 0.

## 2. Estado actual (sé honesto al presentar)

| Parte | Estado |
|---|---|
| Tests unitarios (41) | ✅ pasan |
| Tests de integración (17, con PostgreSQL real) | ✅ pasan en local |
| Análisis estático (ruff) | ✅ pasa |
| `terraform validate` | ✅ válido |
| CI en GitHub Actions | ⏳ escrito, aún no ejecutado en GitHub |
| Infraestructura en AWS (EKS, ECR) | ⏳ no creada todavía |
| CD, despliegue, HPA con carga, Grafana | ⏳ escritos, sin ejecutar contra AWS |
| Informe técnico | ⏳ borrador en `docs/INFORME.md` (faltan resultados reales) |

Todo lo marcado ⏳ está implementado pero **no se ha probado de punta a punta**;
no hay que presentarlo como funcionando hasta tener evidencia real.

## 3. ¿Dónde está cada requisito del profesor?

| Requisito | Qué hicimos | Dónde |
|---|---|---|
| 4.1 Git, ramas, PRs, documentación | Una rama por fase y PR hacia `main` | este repo, `docs/RUNBOOK-AWS.md` |
| 4.2 Testing | pytest: unitarias + integración, reporte JUnit y cobertura | `tests/unit/`, `tests/integration/` |
| 4.3 Containerization | Dockerfiles multi-stage sin root; imágenes versionadas por SHA | `app/*/Dockerfile`, `cd.yml` |
| 4.4 Infrastructure as Code | Terraform: EKS, nodos, EBS CSI, repositorios ECR | `infra/terraform/` |
| 4.5 Integración continua | ruff → pytest (con Postgres) → build de imágenes | `.github/workflows/ci.yml` |
| 4.6 Entrega continua | build, push a ECR, despliegue, smoke test, rollback | `.github/workflows/cd.yml`, `scripts/deploy-aws.sh` |
| 4.7 Orquestación | Deployments, Services, probes, recursos, secretos, PDB | `cluster/base/`, `cluster/overlays/aws/` |
| 4.8 Autoscaling | HPA (CPU 70 %, memoria 80 %), prueba de carga con k6 | `tests/load/k6-load.js`, `scripts/demo-hpa.sh` |
| 4.9 Monitoring | Prometheus + Grafana con dashboard propio | `monitoring/` |
| Recuperación ante fallos | Matar pods de backend y de Postgres con tráfico | `scripts/demo-chaos.sh` |
| Seguridad y costos | Secretos aleatorios fuera de git, etiquetas, script de destrucción | `scripts/aws-down.sh` |

## 4. Mapa de carpetas (lo nuevo)

```
.github/workflows/   ci.yml (pruebas) y cd.yml (publicar y desplegar)
infra/terraform/     infraestructura como código para AWS
cluster/base/        manifiestos Kubernetes originales (funcionan en kind)
cluster/overlays/aws/ ajustes para EKS (StorageClass gp2, HPA máx. 8, límites de tasa)
monitoring/          Prometheus, Grafana y su dashboard
scripts/             aws-up.sh, deploy-aws.sh, smoke-test.sh, aws-down.sh, demo-*.sh
tests/unit/          pruebas sin base de datos
tests/integration/   pruebas con PostgreSQL real
tests/load/k6-load.js prueba de carga oficial (k6)
docs/RUNBOOK-AWS.md  crear, actualizar y destruir el entorno paso a paso
docs/INFORME.md      informe técnico (borrador)
```

## 5. Cómo probar el código en tu PC (sin AWS, sin costo)

Necesitas Python 3.12 y, para los tests de integración, Docker.

```bash
python -m venv .venv && source .venv/bin/activate     # en Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
ruff check app tests
pytest tests/unit                                      # 41 tests, no necesitan nada más
```

Tests de integración (levantan un PostgreSQL temporal en el puerto 55432):

```bash
docker run -d --name pg-test -e POSTGRES_USER=kuber -e POSTGRES_PASSWORD=kuber \
  -e POSTGRES_DB=kuber_test -p 55432:5432 postgres:16
REQUIRE_DB=1 TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 pytest tests/integration
docker rm -f pg-test
```

Sin `REQUIRE_DB=1`, si no hay base de datos los tests de integración se saltan.
En CI está activado, así que ahí **fallan** en vez de saltarse.

## 6. Cómo desplegar en AWS (resumen)

Guía completa y problemas conocidos: [`docs/RUNBOOK-AWS.md`](docs/RUNBOOK-AWS.md).

1. Iniciar el **AWS Academy Learner Lab** y pegar las credenciales en
   `~/.aws/credentials` (caducan cada ~4 h).
2. `./scripts/aws-up.sh` crea EKS + ECR con Terraform (12–18 min).
3. Guardar `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` y `AWS_SESSION_TOKEN`
   como *secrets* del repo en GitHub (**nunca** en el código).
4. Merge a `main` → el CI pasa → el workflow **CD** publica y despliega.
5. Al terminar, **siempre**: `./scripts/aws-down.sh`.

### ⚠️ Costos

El Learner Lab tiene un presupuesto limitado (consulta *Used $X of $Y* al
empezar). El entorno encendido cuesta ≈ **0,25 USD por hora**. Un clúster
olvidado encendido gasta ≈ 6 USD al día. **Destruir siempre al terminar.**
Los scripts de destrucción listan al final lo que pudiera seguir cobrando.

## 7. Flujo de trabajo del equipo

- Una rama por tarea (`feat/…`, `fix/…`) y un **Pull Request** hacia `main`.
- Un PR solo se fusiona con el **CI en verde**.
- Nada de credenciales en commits. Los secretos van en GitHub Secrets.
- No subir `*.tfstate` ni `.terraform/` (ya están en `.gitignore`).

## 8. Guion de la demo final (15 min)

1. Cambiar algo visible (por ejemplo la versión de `GET /api/v1/version`).
2. Hacer push y abrir PR.
3. Mostrar el CI ejecutándose.
4. Mostrar la imagen nueva en ECR.
5. Mostrar el despliegue automático en Kubernetes.
6. Abrir la app y comprobar `/api/v1/version`.
7. Lanzar `k6 run -e BASE_URL=http://<balanceador> tests/load/k6-load.js`.
8. Mostrar el HPA (`scripts/demo-hpa.sh`) y el dashboard de Grafana.
9. Explicar cómo se aprovisiona y cómo se reproduce (`aws-up.sh`, `aws-down.sh`).

La spec pide **evidencia real de ejecución**, no solo capturas de código.

## 9. Limitaciones conocidas

- El Learner Lab no permite crear roles IAM: se reutiliza `LabRole`, así que no
  se aplica mínimo privilegio completo (se explica en el informe).
- Las `NetworkPolicy` existen pero EKS no las aplica sin activar soporte en el
  CNI.
- Hallazgos menores de la app (sin corregir): los 500 de excepciones no
  controladas no se cuentan en las métricas, y `settle_bet` tiene un `return`
  duplicado e inalcanzable.

---

# Documentación original del laboratorio

# bet-exchange-chaos-lab

Entorno de laboratorio de **Apuesta Total (simulado)** sobre Kubernetes, para
observar cómo se comporta un exchange de apuestas ante **carga alta** y **fallos**.

> **SIMULADO.** No es el producto real ni un clon suyo. Los datos son sintéticos
> y los nombres de clubes y usuarios se generan por combinación. No hay
> apuestas ni dinero reales.

No es un clon del producto. Es un sistema que **se comporta como si lo fuera**:
misma topología, mismos servicios, mismas condiciones de operación. El valor
está en que el sistema se sostiene, o se rompe de forma interesante, cuando le
quitas un componente en mitad de tráfico.

> **Para probarlo:** [`docs/COMO-PROBAR.md`](docs/COMO-PROBAR.md)

**Estado:** probado de punta a punta sobre `kind`. El esquema y las
transacciones fueron validados contra PostgreSQL real con consistencia de saldo
garantizada (`drift = 0`), el Ingress Traefik y networking están operativos, y
el arnés de pruebas de carga y caos está calibrado.

---

## Arranque rápido

```bash
make check         # qué falta (Docker corriendo, kind instalado, 8 GB de RAM)
make cluster-up    # crea el cluster, instala métricas, despliega todo
make status        # ¿todo Ready?
open http://localhost:8080
```

Después, para cargar:

```bash
make seeds         # dataset sintético determinista
make seeds-check   # verifica que dos ejecuciones dan lo mismo
make test-stress   # escalabilidad bajo carga
make test-chaos    # tolerancia a fallos
```

Los requisitos y las seis capas de prueba están en
[`docs/COMO-PROBAR.md`](docs/COMO-PROBAR.md).

---

## Qué es

Un exchange de apuestas tiene una propiedad que lo hace un buen banco de pruebas:
cada apuesta toca **varias tablas a la vez** — saldo, ledger, apuesta y
notificación — y eso significa que un fallo a medio camino no es un error, es
dinero que aparece o desaparece. Aquí se puede provocar ese fallo a voluntad y
medirlo.

Lo que se demuestra:

| Eje | La pregunta |
|---|---|
| **Escalabilidad** | ¿El sistema aguanta el equivalente a un inicio de partido con picos? |
| **Pruebas de estrés** | ¿Dónde está el techo real, y es el backend o la base de datos? |
| **Tolerancia a fallos** | Si un servicio se cae, ¿se degrada o se cae entero? |

### Los fallos que duelen de verdad

Un experimento de caos genérico no prueba gran cosa. Estos son los escenarios
que sí importan en un exchange, y los que el proyecto mide:

| Escenario | Qué demuestra |
|---|---|
| **Caída del servicio de pagos** | Degradación parcial, no caída total |
| **Liquidación reintentada** | Idempotencia: el usuario no cobra dos veces |
| **Saldo vs ledger** | Que no se pierde ni se inventa dinero bajo carga |
| **Notificaciones retrasadas** | Que el core no depende de los servicios no críticos |
| **Ráfaga en inicio de partido** | Escalado y límite de la base de datos |

---

## Arquitectura

```
                       ┌──────────────── kuber-app ────────────────┐
  usuario ──▶ :8080 ──▶│  Ingress (traefik)                        │
                       │     /api ──▶ backend (FastAPI, ×2→10 + HPA)│
                       │     /     ──▶ frontend (nginx, ×2 + HPA)  │
                       └────────────────────┬───────────────────────┘
                                            │ TCP 5432
                       ┌──────────────── kuber-data ───────────────┐
                       │  postgres (StatefulSet, PVC, PDB)         │
                       └───────────────────────────────────────────┘

                       ┌──────────────── kuber-observe ────────────┐
                       │  loadgen (generador de carga, ×0 en reposo)│
                       └───────────────────────────────────────────┘
```

Los tres namespaces están aislados: `default-deny` en `kuber-app` y
`kuber-data`, con una regla `allow` explícita por cada flujo. El backend solo
puede hablar con Postgres; Postgres solo acepta tráfico del backend.

## Los 4 contenedores

| # | Contenedor | Tipo | Réplicas | Papel |
|---|-----------|------|----------|-------|
| 1 | `kuber-frontend` | Deployment | 2 → 6 | nginx: estáticos + proxy + rate limiting |
| 2 | `kuber-backend` | Deployment | 2 → 10 | FastAPI: API del exchange, pool de Postgres, circuit breaker |
| 3 | `kuber-postgres` | StatefulSet | 1 | PostgreSQL 16 con volumen persistente |
| 4 | `kuber-loadgen` | Deployment | 0 | generador de carga para las pruebas |

## El dominio

Ocho tablas que modelan un exchange (`app/backend/schema.sql`):

```
events ──┬── markets ──┬── selections ── odds        (libro de precios versionado)
         │             │
         │             └── bets ──┬── transactions   (ledger append-only)
         │                        └── notifications
         └── users ── wallets
```

Dos decisiones que condicionan todo lo demás:

**El saldo no está en una columna, está en el ledger.** `wallets.available` es
un caché; la verdad es `SUM(transactions.amount)`. El endpoint de wallet
devuelve un `drift` = `available - ledger_sum`, y ese número **tiene que
quedarse en 0** aunque hayaplacing miles de apuestas. Es lo que demuestra que
no se ha perdido ni inventado dinero.

**Las cuotas están versionadas.** Cada apuesta guarda el `odds_id` que vio al
colocarse, así que liquidar a las 20:00:05 paga al precio de las 20:00:05, no
al actual. Sin historial, una liquidación correcta sería indistinguible de una
que usó el precio equivocado.

## Las semillas

El dataset se genera, no se escribe a mano (`app/backend/seed.py`):

```bash
make seeds        # 40 eventos, 5 en vivo, 300 usuarios, ~2000 cuotas
make seeds-check  # verifica que dos ejecuciones dan lo mismo
```

Es **determinista**: la misma semilla produce exactamente el mismo dataset. Sin
eso, dos ejecuciones de una prueba no son comparables, porque cualquier
diferencia en latencia es ruido en vez de señal.

Las cuotas de los eventos en vivo **reaccionan al marcador y al reloj**. Un 3-0
en el minuto 15 da OVER a 1.13; un 0-0 en el minuto 67 da UNDER a 1.37. Un
exchange donde el precio no depende del partido no se parece a nada real.

Los datos son inventados. Nombres de clubes y usuarios se generan por
combinación a partir de un vocabulario.

---

## Decisiones de diseño

### Las tres sondas, separadas

Es la decisión más importante del proyecto:

| Sonda | Comprueba la BD | Si falla |
|-------|----------------|----------|
| `startupProbe` | no | desactiva las otras dos, da margen al arranque |
| `livenessProbe` | **no** | **reinicia** el pod |
| `readinessProbe` | sí | **saca** el pod del Service, no lo reinicia |

`livenessProbe` **no** toca la base de datos a propósito. Si la comprobara, una
caída de PostgreSQL reiniciaría todos los pods del backend a la vez, y el
incidente se volvería autoinculpable: reiniciar un proceso que solo esperaba a
la base de datos no arregla nada, solo retrasa el recuperación.

`readinessProbe` sí la comprueba, con timeout corto. Si falla, el pod sale del
Service y el sistema **degrada en lugar de caerse**. La app sigue sirviendo lo
que no depende de la base de datos.

### El `preStop` con `sleep 15`

No es un desperdicio. El kubelet ejecuta `preStop` **antes** de mandar
`SIGTERM`, así que ese sleep da tiempo a que kube-proxy actualice los endpoints
en todos los nodos. Sin él, el tráfico que aún apunta al pod que muere recibe
`ECONNREFUSED` justo cuando ese pod ya no puede atender. Es el patrón estándar
de *drain*.

`terminationGracePeriodSeconds: 45`, por encima del `timeout-keep-alive` de
uvicorn (20 s), para que nunca se corte una petición sana.

### Requests y limits

```yaml
requests: { cpu: 200m, memory: 256Mi }   # reserva garantizada
limits:   { cpu: 1000m, memory: 512Mi }  # techo duro
```

Los **requests son el parámetro más importante del HPA**, porque
`utilization = uso / request`. Con `averageUtilization: 70` y un request de
200m, el HPA escala cuando la media de los pods pasa de ~140m. Request demasiado
alto y nunca escala; demasiado bajo y escala antes de tiempo.

PostgreSQL va **sin límite de CPU** a propósito: durante las pruebas de estrés
necesita CPU libre para responder rápido, y con límite el CFS lo estrangula
contra sí mismo.

### HPA asimétrico

Sube al 100 % cada 30 s (reactivo ante picos), pero baja 1 pod cada 60 s con
ventana de 300 s (conservador). Sin esa asimetría aparece el oscilo clásico
escala → baja → escala → baja cuando la carga oscila cerca del umbral, y ese
movimiento es lo que más cuesta.

El HPA de **memoria** es prácticamente irreversible: la memoria casi nunca baja,
así que una vez que sube por memoria el `scaleDown` no suele activarse. Está
bien como alarma de fuga, mal como mecanismo de ajuste.

### Apuesta atómica

`POST /bets` toca wallet, ledger, apuesta y notificación **en una sola
transacción**. El saldo se descuenta con `UPDATE ... WHERE available >= ?`: si
no alcanza, la sentencia no toca fila alguna y `rowcount` es 0. Eso es un
rechazo atómico (402) en una sola sentencia, no un `SELECT` seguido de un
`UPDATE` que dejaría una carrera entre dos apostas del mismo usuario.

`POST /bets/{id}/settle` usa `SELECT ... FOR UPDATE` sobre la apuesta más un
índice único en `liquidation_key`. Un reintento choca contra el índice y
devuelve 409 en vez de pagar dos veces. La clave se deriva de `(bet_id,
resultado)`, no de un UUID por intento: un UUID nuevo en cada reintento dejaría
pasar todos.

### PDB: `maxUnavailable: 0` en la base de datos

Impide que un drain de nodo tumbe PostgreSQL. El coste es que el drain **se
bloquea** si el pod está ahí: con una sola réplica no hay a dónde ir. Es el
precio de no perder la garantía de cero caída.

---

## Limitaciones

- **`kind` no es producción.** Los 4 nodos son contenedores Docker en tu
  máquina. Las cifras de RPS no son extrapolables.
- **PostgreSQL con una sola réplica.** Sin réplica de lectura ni failover. Si el
  nodo de datos muere, hay pérdida de datos.
- **Sin registry.** Las imágenes se cargan con `kind load`, así que solo
  funcionan en local.
- **nginx sin keepalive al upstream.** En la versión open source `resolve` en un
  bloque `upstream` no existe (es NGINX Plus). Se usa una variable para
  re-resolver por DNS, pero eso impide el keepalive.
- **El HPA de CPU necesita metrics-server**, que kind no trae de serie
  (el comando `make cluster-up` lo instala automáticamente).

---

## Estructura

```
cluster/
  kind-config.yaml            cluster de 4 nodos
  base/                       manifiestos, en orden de aplicación
    00-namespace.yaml         namespaces, ResourceQuota, LimitRange
    01-configmap.yaml         configuración no sensible
    02-secret.yaml            secretos (SOLO dev)
    03-postgres.yaml          StatefulSet, PDB, NetworkPolicy
    04-backend.yaml           Deployment, Service, HPA, PDB, NetworkPolicy
    05-frontend.yaml          Deployment, Service, HPA, PDB
    06-ingress.yaml           Ingress + Middleware de traefik
app/
  backend/
    app.py                    FastAPI: API, sondas, circuit breaker
    schema.sql                esquema de dominio
    seed.py                   generador de semillas determinista
    seed_load.py              cargador al Postgres (COPY, con advisory lock)
  frontend/
    nginx.conf                proxy, rate limiting, sondas
    index.html                panel de control del dominio
scripts/
  autopilot.sh                piloto automático de los 3 ejes
tests/
  load/load.py                generador de carga (con fase de descubrimiento)
  load/loadgen.yaml           su Deployment
docs/
  COMO-PROBAR.md              guía de pruebas
Makefile
```

---

## Aviso

Este proyecto es un **simulador con fines de aprendizaje**. No está afiliado a
ninguna casa de apuestas. Los nombres de clubes, competiciones y usuarios se
generan por combinación a partir de un vocabulario y **no corresponden a
entidades ni participantes reales**. Los precios, partidos y resultados son
inventados.

Las cuotas generadas no tienen valor predictivo ni reflejan ningún mercado real.
