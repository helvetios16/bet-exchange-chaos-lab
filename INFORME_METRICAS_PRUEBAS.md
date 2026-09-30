# Informe Técnico Consolidado: Métricas de Pruebas y Validación de Arquitectura

**Proyecto**: Bet Exchange Chaos Lab  
**Entorno**: Kubernetes (Kind) sobre macOS  
**Fecha de consolidación**: 30 de Septiembre de 2026  
**Fuentes de telemetría**: `reports/run-20260930-073243`, `reports/run-20260930-083317`, `reports/run-20260930-084238`, `reports/run-20260930-091606`, `reports/run-20260930-095847`, `reports/run-20260930-100538`

---

## 1. Resumen Ejecutivo

El presente informe consolida y analiza las métricas obtenidas durante las campañas de pruebas de estrés, caos y resistencia ejecutadas sobre la plataforma de apuestas. Las pruebas validaron la capacidad del sistema para cumplir los tres pilares no funcionales exigidos por la arquitectura:

1. **Escalabilidad Elástica (Eje 1)**: El Horizontal Pod Autoscaler (HPA) detectó la saturación de CPU y escaló réplicas en un lapso de 90 a 100 segundos, manteniendo la estabilidad operativa bajo ráfagas de hasta 150-200 RPS.
2. **Alta Disponibilidad y Tolerancia a Fallos (Eje 2)**: Durante la inyección de fallos críticos (muerte de pod por SIGKILL, reinicio de base de datos, drenado de nodo trabajador y despliegue continuo de frontend), se registraron 2,850 peticiones continuas con **0% de tasa de fallo (100% de disponibilidad)** para los usuarios.
3. **Integridad Financiera y Resistencia Temporal (Eje 3)**: El consumo de memoria se mantuvo acotado entre 51 MB y 66 MB por pod sin evidencia de fugas (leaks). Las transacciones de liquidación duplicada fueron interceptadas con código HTTP 409, garantizando un **drift financiero de exactamente 0.00 EUR** sobre 300 billeteras auditadas.

---

## 2. Parámetros del Entorno de Evaluación

| Componente | Especificación / Versión | Rol en la Arquitectura |
| :--- | :--- | :--- |
| **Cluster Kind** | 4 Nodos (1 Control Plane, 3 Workers) | Aislamiento de cargas de trabajo y simulación multi-nodo |
| **Ingress Controller** | Traefik v3 con middleware de reintentos | Balanceo round-robin y reintento automático de peticiones |
| **Backend API** | Python / Flask / psycopg3 | Capa transaccional con pools de conexión dedicados |
| **Base de Datos** | PostgreSQL 16 (StatefulSet) | Registro contable con índices de unicidad para liquidación |
| **Generador de Carga** | `kuber-loadgen` (Go / HTTP client) | Generación concurrente de tráfico en nodo dedicado (`kuber-worker3`) |
| **Telemetría** | Kubernetes Metrics Server + scripts de captura | Muestreo de CPU/RAM por pod cada 5 segundos |

---

## 3. Eje 1: Carga y Escalabilidad Horizontal (HPA)

### 3.1 Datos Analizados
- Fuentes: [stress.csv (run-091606)](file:///Users/sebastian/Documents/Variety/bet-exchange-chaos-lab/reports/run-20260930-091606/stress.csv) y [stress.csv (run-095847)](file:///Users/sebastian/Documents/Variety/bet-exchange-chaos-lab/reports/run-20260930-095847/stress.csv)
- Duración por prueba: 295 segundos (5 minutos por corrida)
- Muestras recolectadas: 60 intervalos de 5 segundos

### 3.2 Comportamiento de CPU y Autoscaling

```
CPU (milis)
  200 |                                              XXXXXXXXX (Pico: 174.6m)
  150 |                                      XXXXXXXX
  140 | ------------------------------------- (Umbral HPA: 70% = 140m)
  100 |                              XXXXXXXX
   50 |                      XXXXXXXX
    0 | XXXXXXXXXXXXXXXXXXXXX
      +--------------------------------------------------------> Tiempo
        0s        30s        60s        90s        120s    295s
                                         ^
                                   Escalado HPA
                                 (2 -> 3 réplicas)
```

### 3.3 Tabla Comparativa de Rendimiento HPA

| Métrica | Corrida 09:16:06 | Corrida 09:58:47 | Diagnóstico |
| :--- | :--- | :--- | :--- |
| **CPU Reposo (Línea Base)** | 3.23 m / pod | 11.73 m / pod | Consumo nominal sin tráfico |
| **CPU Pico Promedio** | 168.72 m / pod | 174.61 m / pod | Superó el límite de activación (140 m) |
| **CPU Promedio Global** | 117.70 m / pod | 126.93 m / pod | Rango de operación controlada |
| **Consumo RAM Promedio** | 53.70 MB | 53.09 MB | Estable frente a picos de CPU |
| **Tiempo al Primer Disparo** | 90 segundos | 100 segundos | Ventana de estabilización HPA respetada |
| **Réplicas Iniciales / Finales** | 2 -> 3 | 2 -> 3 | Expansión elástica sin saturación |
| **Pods Ready Durante Transición** | 2 -> 3 | 2 -> 3 | 0 segundos de interrupción |

---

## 4. Eje 2: Resiliencia y Tolerancia a Fallos (Chaos Engineering)

### 4.1 Datos Analizados
- Fuente: [resumen.csv (run-083317)](file:///Users/sebastian/Documents/Variety/bet-exchange-chaos-lab/reports/run-20260930-083317/resumen.csv) y registros detallados `chaos-*.csv`
- Total de transacciones sometidas a inyección de fallos: 2,850 peticiones

### 4.2 Matriz de Resultados de Caos

| Escenario de Inyección | Carga Inyectada | Errores 5xx | Tasa de Fallo (%) | Mecanismo de Defensa Validado |
| :--- | :--- | :--- | :--- | :--- |
| **Muerte de Pod (`SIGKILL`)** | 520 req | 0 | 0.00% | Middleware `kuber-retry` de Traefik y reemplazo inmediato por ReplicaSet |
| **Caída de PostgreSQL** | 1,030 req | 0 | 0.00% | Probe de Readiness desacoplada; reconexión automática del pool `psycopg3` |
| **Drenado de Nodo (`kubectl drain`)** | 780 req | 0 | 0.00% | `PodDisruptionBudget` (`minAvailable: 1`) impidió la expulsión masiva simultánea |
| **Despliegue Continuo (Frontend)** | 520 req | 0 | 0.00% | Rolling update con `maxSurge: 1` y `maxUnavailable: 0` |
| **TOTAL CONSOLIDADO** | **2,850 req** | **0** | **0.00%** | **Disponibilidad 100.00% bajo estrés destructivo** |

### 4.3 Análisis de Recuperación
- **Recuperación tras SIGKILL**: El pod caído fue retirado del endpoint del servicio en menos de 200 ms por kube-proxy. El pod sustituto alcanzó el estado `Running` y superó la prueba de salud en 2.3 segundos.
- **Resiliencia ante corte de base de datos**: Durante los 5 segundos de reinicio del pod `kuber-postgres-0`, las peticiones entrantes fueron puestas en cola o atendidas de forma transaccional sin pérdidas ni escrituras parciales.

---

## 5. Eje 3: Resistencia Temporal e Integridad Financiera

### 5.1 Datos Analizados
- Fuentes: [soak.csv (run-084238)](file:///Users/sebastian/Documents/Variety/bet-exchange-chaos-lab/reports/run-20260930-084238/soak.csv) y telemetría de consistencia contable
- Muestras evaluadas: Monitoreo continuo de memoria y balance de transacciones

### 5.2 Estabilidad de Memoria (Ausencia de Memory Leaks)

| Ventana de Tiempo | Memoria Promedio (KiB) | Memoria Máxima (KiB) | Variación |
| :--- | :--- | :--- | :--- |
| **Minuto 0:00 (Inicio)** | 51,116 KiB (~49.9 MB) | 51,116 KiB | Línea base |
| **Minuto 1:00 (Carga media)** | 62,508 KiB (~61.0 MB) | 64,294 KiB | Almacenamiento de buffers |
| **Minuto 2:30 (Carga alta)** | 60,612 KiB (~59.2 MB) | 66,788 KiB | Meseta estable |
| **Minuto 3:00 (Fin de ciclo)** | 59,796 KiB (~58.4 MB) | 61,829 KiB | Liberación por Garbage Collector |

*Conclusión de memoria*: La memoria RAM no presentó crecimiento monótono. Permaneció confinada en un canal de entre 50 MB y 66 MB por contenedor, muy por debajo del límite de 256 MiB asignado en el `resources.limits`.

### 5.3 Auditoría de Integridad Financiera

Se verificaron dos propiedades críticas del motor de transacciones:

1. **Prevención de Doble Cobro (Idempotencia)**:
   - Prueba: Envío repetido del endpoint `/api/v1/bets/{id}/settle` con el mismo ID de liquidación.
   - Resultado: 100% de los reintentos duplicados fueron rechazados con `HTTP 409 Conflict` gracias a la restricción `UNIQUE (bet_id)` en la tabla `liquidations`.
2. **Invariante Contable (Zero Drift)**:
   - Fórmula auditada:
     $$\text{Drift} = \text{Saldo Disponible} - \sum(\text{Movimientos en Ledger})$$
   - Muestra auditada: 300 billeteras de prueba con más de 8,900 apuestas transaccionadas.
   - **Resultado de drift**: **0.00 EUR** (Exactitud matemática absoluta).

---

## 6. Cuadro de Mando de KPIs Consolidados

| Indicador Clave (KPI) | Objetivo de Diseño | Valor Observado en Pruebas | Cumplimiento |
| :--- | :--- | :--- | :--- |
| **Disponibilidad en Caos** | >= 99.90% | **100.00%** (2,850/2,850 OK) | Superado |
| **Tiempo de Detección HPA** | < 120 segundos | **90 - 100 segundos** | Cumplido |
| **Límite de Consumo de RAM** | < 256 MiB / pod | **66.8 MiB pico** | Cumplido (Margen holgado) |
| **Fugas de Memoria (Leak)** | 0 crecimiento monótono | **Meseta plana verificada** | Cumplido |
| **Idempotencia de Liquidaciones**| 100% bloqueo de doble pago | **HTTP 409 en colisiones** | Cumplido |
| **Drift Contable en Billeteras** | == 0.00 EUR | **0.00 EUR** | Cumplido |

---

## 7. Conclusiones Técnicas

1. La combinación de **Traefik Ingress con reintentos transparentes** y **PodDisruptionBudget en Kubernetes** eliminó por completo los errores 5xx perceptibles para los clientes durante incidentes de infraestructura.
2. El desacoplamiento entre las sondas de **Liveness** (supervivencia del proceso) y **Readiness** (conectividad a la base de datos) previno caídas en cascada de pods cuando PostgreSQL fue reiniciado.
3. El motor de liquidaciones demostró consistencia estricta frente a ataques de condición de carrera y reintentos agresivos de red.

---
*Documento generado automáticamente a partir de los datos consolidados en el directorio `reports/`.*
