# Cómo probarlo

Guía de pruebas del entorno Apuesta Total (simulado), **por capas**.

El orden importa. Si algo de abajo falla, lo de arriba produce números que no
significan nada: una prueba de carga sobre un esquema que no ha arrancado
mide el arranque, no el sistema.

> Nada de esto se ha ejecutado todavía. Esta guía describe lo que habría que
> hacer, en el orden en que hay que hacerlo.

---

## Antes de empezar: requisitos

### Software

| Requisito | Para qué |
|---|---|
| **Docker Desktop, daemon corriendo** | kind levanta el cluster como contenedores |
| **kind** (`brew install kind`) | crea el cluster local de 4 nodos |
| `kubectl` | hablar con el cluster |
| `jq` | el autopilot lo usa para leer métricas |
| `python3` | generar las semillas |
| `make` | los objetivos del proyecto |

`make check` comprueba qué falta:

```bash
make check
```

### La máquina

| Recurso | Mínimo | Comentario |
|---|---|---|
| CPU | 4 núcleos | los 4 nodos de kind la comparten |
| RAM | 8 GB asignados a Docker | **el requisito que más se olvida** |
| Disco | 10 GB | |

**Docker Desktop reserva poca memoria por defecto**, y con la de por defecto el
clúster muere con `OOMKilled` sin dar mensajes útiles. Hay que subirlo a **8 GB
mínimo** en *Settings → Resources → Memory*. Si no, los pods mueren solos a
mitad de una prueba y parece un bug del diseño cuando es la configuración de
Docker.

---

## Capa 1 — Que arranque

Lo primero, y lo que más falla. Si aquí hay problemas, no seguir.

```bash
make cluster-up
```

Ese objetivo hace, en orden:

1. `kind create cluster` con 4 nodos (1 control-plane + 3 workers)
2. instala **metrics-server** (imprescindible para el HPA)
3. construye las 3 imágenes y las carga al cluster con `kind load`
4. aplica los manifiestos de `cluster/base/`, en orden
5. genera y publica las semillas
6. espera a que todo esté `Ready`

Después:

```bash
make status
```

Se espera ver: Postgres `1/1`, backend `2/2`, frontend `2/2`, y el HPA del
backend con `TARGETS` poblado en lugar de `<unknown>`.

### Si falla

| Síntoma | Causa probable |
|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop no está arrancado |
| Pods `Pending` sin motivo aparente | RAM de Docker Desktop por debajo de 8 GB |
| Postgres en `CrashLoopBackOff` | ver capa 2 |
| HPA en `<unknown>` | metrics-server no instalado → `make metrics-server` |

Para ver qué pasó realmente:

```bash
make logs            # logs del backend
kubectl -n kuber-data get pods
kubectl -n kuber-data logs statefulset/kuber-postgres
```

---

## Capa 2 — El SQL (la capa con más riesgo)

**Es la capa que más conviene comprobar primero, y la que menos está probada.**
Todo el esquema y las transacciones están escritos pero no se han ejecutado
contra un Postgres real.

```bash
make psql            # ¿el esquema se crea sin errores?
```

Si el `schema.sql` se aplica limpio, sigue:

```bash
make seeds           # genera el dataset (40 eventos, 300 usuarios)
make restart-backend # reinicia el backend para que las cargue
```

Verificar que se cargó:

```bash
kubectl -n kuber-app logs deploy/kuber-backend | grep '\[seed\]'
```

Debería verse `dataset cargado` con los conteos. Si sale `dataset ya cargado
por otro pod`, es que otro pod ganó la carrera del advisory lock, que es el
comportamiento esperado con más de una réplica.

### Comprobar la coherencia del dataset a mano

```bash
make psql
```

```sql
-- no debería devolver filas: mercados sin selecciones
SELECT count(*) FROM markets m
  WHERE NOT EXISTS (SELECT 1 FROM selections s WHERE s.market_id = m.id);

-- no debería devolver filas: cuotas sin mercado
SELECT count(*) FROM odds o
  WHERE NOT EXISTS (SELECT 1 FROM markets m WHERE m.id = o.market_id);

-- no debería devolver filas: todos los usuarios con wallet
SELECT count(*) FROM users u
  WHERE NOT EXISTS (SELECT 1 FROM wallets w WHERE w.user_id = u.id);

-- no debería devolver filas: apuestas sin usuario o sin mercado válido
SELECT count(*) FROM bets b
  WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = b.user_id)
     OR NOT EXISTS (SELECT 1 FROM markets m WHERE m.id = b.market_id);

-- coherencia de las cuotas en vivo: si el local va ganando, su cuota debe
-- ser MÁS BARATA que la del visitante. Si devuelve filas, el generador de
-- semillas tiene el signo invertido.
SELECT count(*) AS incoherentes
FROM events e
JOIN markets  m   ON m.event_id = e.id AND m.market_type = 'MATCH_1X2'
JOIN selections sh ON sh.market_id = m.id AND sh.code = 'HOME'
JOIN selections sa ON sa.market_id = m.id AND sa.code = 'AWAY'
-- último precio conocido de cada lado
JOIN LATERAL (
  SELECT back_price FROM odds o
  WHERE o.selection_id = sh.id ORDER BY o.created_at DESC LIMIT 1
) ph ON TRUE
JOIN LATERAL (
  SELECT back_price FROM odds o
  WHERE o.selection_id = sa.id ORDER BY o.created_at DESC LIMIT 1
) pa ON TRUE
WHERE e.status = 'IN_PLAY'
  AND e.score_home > e.score_away
  AND ph.back_price > pa.back_price;
```

### Si falla

Los fallos aquí son esperables la primera vez, porque el SQL está sin ejecutar.
Lo más probable: un tipo que no acepta lo que se le inserta, o una restricción
que el generador no respeta.

---

## Capa 3 — El sistema responde

```bash
open http://localhost:8080
```

El panel tiene cinco botones. En orden:

| Botón | Qué tiene que pasar |
|---|---|
| **Listar eventos en vivo** | devuelve eventos `IN_PLAY` con marcador y minuto |
| **Ver cuotas de un mercado** | precios que cuadran con el marcador |
| **Colocar una apuesta** | `200` con un `bet_id`, o `402` si no hay saldo |
| **Consultar agregados** | apuestas abiertas, ganadas, perdidas, volumen |
| **Auditar saldo de un usuario** | **`drift` en 0** |

El de **drift** es el importante. Es `available - SUM(ledger)`. Tiene que dar
0 tras colocar apuestas. Si da otra cosa, hay una fuga de dinero y hay que
parar ahí antes de seguir.

También el estado de arriba: los health checks deben estar todos en verde.
Si `/readyz` está en rojo pero `/healthz` en verde, es la base de datos la que
no responde, y el frontend está haciendo exactamente lo que debe.

---

## Capa 4 — Una carga pequeña primero

No empezar por `make test-stress`. Primero algo que quepa en una pantalla:

```bash
MAX_RPS=50 RAMP_SECONDS=30 HOLD_SECONDS=30 make test-stress
```

Genera un informe en `reports/run-<timestamp>/`:

```
resumen.txt / resumen.csv     tabla de resultados
stress.csv                    métricas de escalabilidad
chaos-*.csv                   códigos HTTP durante cada fallo
cluster-*.log                 estado del cluster antes y después
```

### Cómo leer `stress.csv`

```
ts,elapsed_s,hpa_current,cpu_pods_mean,cpu_pods_max,ready_pods,total_pods
11:20:04,0,2,38,71,2,2
11:20:34,30,2,94,180,2,2
11:21:04,60,3,112,240,3,3
```

Qué mirar:

- **`hpa_current` sube y `cpu_pods_mean` baja** → el escalado reparte bien
- **los RPS suben pero la latencia también** → estás añadiendo pods sin
  resolver el cuello, y el cuello casi siempre es Postgres
- **`cpu_pods_mean` se estabiliza mientras `ready_pods` sigue subiendo** → ahí
  está tu punto de inflexión
- **`hpa_current` se queda en 2** → o no hay carga suficiente, o falta
  metrics-server. Comprobar con `make describe-hpa`

### El techo de 500 rps

Con `MAX_RPS=500` el HPA probablemente **se quede en 2 o 3 réplicas**. Es lo
esperado y no es un fallo: significa que 500 rps no es carga suficiente para
escalar.

Subirlo a 3000 mediría otra cosa: la base de datos satura antes y el resultado
es el techo de Postgres, no el del backend. Para el objetivo del experimento,
500 rps y números estables dan más información que 3000 rps y números
inestables.

---

## Capa 5 — Tolerancia a fallos

Aquí está el corazón del experimento.

```bash
make test-chaos
```

Cuatro escenarios, cada uno midiendo **cuántas peticiones se pierden de
verdad** (no si el pod se reinició, que es lo que suele mirarse por error):

| # | Escenario | Resultado esperado |
|---|-----------|--------------------|
| 2.1 | Matar un pod del backend con tráfico | **0 errores**. El Service retira el endpoint y traefik reintenta |
| 2.2 | Reiniciar PostgreSQL con tráfico | Errores inevitables, **pero 0 reinicios en cascada** del backend |
| 2.3 | Cordon + drain de un nodo | El PDB bloquea el drain si rompería la garantía |
| 2.4 | Rollout del frontend | **0 errores**. `maxUnavailable: 0` + `preStop` |

### El escenario 2.2 es el que más informa

Es donde se ve si el diseño de sondas está bien:

- Si `liveness` comprueba **solo el proceso** → la caída de la base de datos
  saca los pods del Service (degradación) sin reiniciarlos.
- Si `liveness` comprobara **la base de datos** → todos los pods se reiniciarían
  a la vez y un incidente de dependencia se convertiría en una caída total.

El script detecta ese segundo caso explícitamente y lo reporta como **fallo**,
con el mensaje: `el backend se reinició en cascada al caer la BD`. Si aparece,
la causa está en `livenessProbe` de `cluster/base/04-backend.yaml`, y debería
apuntar a `/health/live` y no a `/health/ready`.

Lo que hay que mirar además:

- **RTO**: cuánto tarda el servicio en recuperarse solo. El script mide si la
  BD vuelve sin intervención.
- **Reinicios de contenedor**: si pasan de 0 a varios, es el fallo en cascada.

### Idempotencia de liquidación

No está en el autopilot, se prueba a mano desde el panel o con `curl`:

```bash
BET_ID=$(kubectl -n kuber-app exec deploy/kuber-backend -- \
  curl -s localhost:8080/api/v1/stats | jq -r '.open_bets' )

# liquidar la misma apuesta dos veces
curl -s -XPOST localhost:8080/api/v1/bets/$BET_ID/settle \
  -H 'Content-Type: application/json' -d '{"won":true}'
# → 200 la primera
curl -s -XPOST localhost:8080/api/v1/bets/$BET_ID/settle \
  -H 'Content-Type: application/json' -d '{"won":true}'
# → 409 la segunda, y el saldo NO puede haber cambiado
```

El 409 es el resultado correcto. Si devuelve 200 dos veces, el índice único de
`bets.liquidation_key` no está funcionando y hay un bug de pago doble.

---

## Capa 6 — Endurance

```bash
SOAK_MINUTES=60 make test-soak
```

Carga moderada durante una hora. Busca fallos que solo aparecen con el tiempo:

**Si las réplicas del HPA suben solas con la carga constante**, hay una **fuga
de memoria** en el backend. Es el hallazgo más fácil de esta capa, y el que
justifica hacerla.

---

## Orden recomendado

Si solo vas a hacer una pasada rápida:

```bash
make check                 # requisitos
make cluster-up            # capa 1
make status                # ¿todo Ready?
make psql                  # capa 2 — el SQL
make restart-backend
make seeds                 # y comprobar que se cargan
open http://localhost:8080  # capa 3 — ¿responde? ¿drift 0?
MAX_RPS=50 RAMP_SECONDS=30 HOLD_SECONDS=30 make test-stress   # capa 4
make test-chaos            # capa 5
```

## Limpiar

```bash
make clean                 # borra solo los informes
make undeploy              # borra los manifiestos, conserva los volúmenes
make destroy               # borra todo, incluidos los volúmenes
make cluster-down          # destruye el cluster entero
```
