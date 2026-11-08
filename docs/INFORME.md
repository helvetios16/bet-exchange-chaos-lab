# Informe técnico — Aplicación cloud-native en AWS

> Borrador: las secciones marcadas **[COMPLETAR]** requieren datos reales de
> la ejecución (capturas, cifras de k6, CSV del HPA). No inventar resultados.

## 1. Resumen

Aplicación de laboratorio que simula un exchange de apuestas (FastAPI + nginx
+ PostgreSQL) desplegada en Kubernetes sobre AWS. Se añadieron: pruebas
automatizadas, CI/CD con GitHub Actions, infraestructura como código con
Terraform, registry en ECR, autoscaling con HPA y monitorización con
Prometheus y Grafana.

## 2. Arquitectura

```
Desarrollador → GitHub (PR) → CI (ruff, pytest unit+integración, build Docker)
                                 │ verde en main
                                 ▼
                            CD → ECR (imagen:SHA) → kubectl apply (EKS)
                                                         │
Usuario → Balanceador AWS → Traefik → backend ×2..8 (HPA) → PostgreSQL (EBS)
                                  └→ frontend ×2..6 (HPA)
Prometheus/Grafana ← /metrics, kube-state-metrics, cAdvisor
```

**[COMPLETAR]** diagrama exportado (draw.io o Mermaid) de infraestructura y de
flujo CI/CD.

## 3. Selección de herramientas y justificación

| Componente | Elegido | Alternativa | Justificación |
|---|---|---|---|
| Control de versiones | Git / GitHub | GitLab | Lo exige el flujo de PRs y Actions |
| Testing | pytest | — | El backend es Python; `TestClient` de FastAPI permite pruebas de API sin levantar servidor |
| Contenedores | Docker (multi-stage, usuario no root) | Podman | Ya existían Dockerfiles; se versiona por SHA |
| Registry | Amazon ECR | GHCR | Integrado con EKS y IAM; escaneo al subir |
| CI/CD | GitHub Actions | Jenkins | Sin servidor que mantener; service containers para Postgres |
| IaC | Terraform | OpenTofu | Herramienta de referencia de la asignatura |
| Orquestación | Amazon EKS | k3s | Preferente en la spec; el Learner Lab permite crear el clúster con `LabRole` |
| Monitorización | Prometheus + Grafana | CloudWatch | La app ya expone métricas Prometheus; paneles reproducibles por código |
| Carga | k6 | Locust | Scripts en JS, umbrales declarativos y salida de métricas |

## 4. Decisiones de diseño relevantes

- **VPC por defecto y nodos en subredes públicas:** evita NAT Gateway
  (~0,045 USD/h) y simplifica el laboratorio. En producción: subredes privadas.
- **LabRole en lugar de roles propios:** el Learner Lab no permite crear roles
  IAM. Limita el principio de mínimo privilegio; se documenta como restricción
  del entorno.
- **Sondas separadas:** la *liveness* no consulta la BD, así una caída de
  PostgreSQL saca los pods del Service (readiness) pero no los reinicia en
  cascada. Está cubierto por pruebas unitarias.
- **Secretos:** generados aleatoriamente por el script de despliegue y
  guardados solo en el clúster; las credenciales AWS viven en GitHub Secrets.
- **Límites de tasa del overlay AWS:** la prueba de carga sale de una sola IP;
  con los valores del entorno local Traefik devolvería 429 antes de que suba la
  CPU y el HPA no se activaría.
- **Consistencia financiera:** el saldo se verifica con `drift = wallet −
  SUM(ledger) = 0`, probado antes y después de cada fallo y de cada carga.

## 5. Pruebas automatizadas

- Unitarias: 40 (redondeo monetario, rate limit, circuit breaker, sondas,
  validación, métricas, generador de semillas).
- Integración: 17 (API + PostgreSQL real: apuestas BACK/LAY, saldo
  insuficiente, liquidación idempotente, drift = 0).
- El CI ejecuta además ruff y construye las imágenes; un test roto detiene el
  pipeline (el job de build depende de lint y test).

**[COMPLETAR]** captura de un PR con tests fallando (pipeline en rojo) y de
uno en verde; reporte JUnit y cobertura del artefacto.

## 6. Evidencias de Kubernetes

**[COMPLETAR]** salida de `./scripts/demo-chaos.sh` (pod del backend
eliminado, Postgres eliminado, reinicios, drift).

## 7. Autoscaling y carga

**[COMPLETAR]** perfil k6 usado, p95/p99, tasa de error, CSV de
`demo-hpa.sh` (réplicas vs tiempo), análisis: tiempo hasta el primer
escalado, réplicas máximas alcanzadas, comportamiento al bajar la carga
(ventana de estabilización de 5 min).

## 8. Monitorización

**[COMPLETAR]** capturas del dashboard *Chaos Lab - Backend y HPA* durante la
prueba de carga.

## 9. Seguridad y costos

- Ninguna credencial en el repositorio; escaneo de imágenes en ECR.
- Recursos etiquetados `Project=chaos-lab`; `scripts/aws-down.sh` destruye y
  verifica que no queden recursos.
- Costo del entorno ≈ 0,25 USD/h. **[COMPLETAR]** gasto real (Used $X of $50).
- Limitación: NetworkPolicy definidas pero no aplicadas por el CNI de EKS por
  defecto.

## 10. Reproducibilidad

Ver `docs/RUNBOOK-AWS.md`: crear (`aws-up.sh`), actualizar (merge a `main`) y
destruir (`aws-down.sh`).

## 11. Hallazgos y mejoras

- Las respuestas 500 de excepciones no controladas no se registran en las
  métricas (el handler genérico corre fuera del middleware).
- `settle_bet` tiene un `return` duplicado e inalcanzable.
- `seed.py` usa la hora actual para los eventos, por lo que el dataset no es
  100 % determinista como afirma el README.
