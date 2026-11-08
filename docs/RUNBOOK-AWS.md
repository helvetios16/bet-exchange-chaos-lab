# Runbook AWS: crear, actualizar y destruir el entorno

Procedimiento reproducible para AWS Academy Learner Lab. Costo estimado del
entorno encendido: **≈ 0,25 USD/hora** (EKS 0,10 + 3 × t3.medium 0,125 +
balanceador 0,03). **Destruir siempre al terminar.**

## 0. Requisitos en tu PC

```bash
winget install Hashicorp.Terraform Helm.Helm k6.k6 GitHub.cli
aws --version && terraform version && kubectl version --client && helm version && k6 version
```

Los scripts son bash: ejecutarlos desde **Git Bash**.

## 1. Credenciales del laboratorio

1. Learner Lab → **Start Lab** → **AWS Details → Show**.
2. Pegar el bloque en `C:\Users\HP\.aws\credentials` (archivo llamado
   exactamente `credentials`, sin `.txt`).
3. `aws sts get-caller-identity` debe responder con el rol `voclabs`.
4. Revisar `Used $X of $50` antes de empezar.

Las credenciales caducan con la sesión (~4 h): repetir al reiniciar el lab.

## 2. Crear la infraestructura

```bash
./scripts/aws-up.sh        # terraform init + apply, y configura kubectl
```

Crea: clúster EKS, grupo de 3 nodos t3.medium, driver EBS CSI y 3
repositorios ECR (`chaos-lab/backend|frontend|loadgen`). Tarda 12–18 min.

## 3. Secretos de GitHub (una vez por sesión de laboratorio)

Repo → Settings → Secrets and variables → Actions:
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`
(los tres del bloque de *AWS Details*). Nada de esto se guarda en el repo.

## 4. Desplegar

Merge a `main` → CI en verde → el workflow **CD** publica las imágenes en ECR
(etiqueta = SHA corto del commit) y ejecuta `scripts/deploy-aws.sh`, que
instala metrics-server, Traefik, secretos aleatorios, la app y la
monitorización, y termina con un *smoke test*. También se puede lanzar a mano:
Actions → CD → *Run workflow*.

URL pública: `kubectl -n traefik get svc traefik` (columna EXTERNAL-IP).

## 5. Actualizar

Cambiar código → PR → merge a `main`. El CD vuelve a construir, aplica el
nuevo tag (rolling update sin caída) y, si el smoke test falla, hace
`rollout undo`.
Cambios de infraestructura: editar `infra/terraform/*.tf` y `./scripts/aws-up.sh`.

## 6. Evidencias para la demo

| Qué | Cómo |
|---|---|
| Recuperación ante fallos | `./scripts/demo-chaos.sh` |
| Carga | `k6 run -e BASE_URL=http://<LB> tests/load/k6-load.js` |
| HPA en vivo | `./scripts/demo-hpa.sh` (guarda CSV en `reports/`) |
| Dashboards | `kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80` → http://localhost:3000, dashboard **Chaos Lab - Backend y HPA** (usuario `admin`; contraseña: `kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' \| base64 -d`) |
| Pods y CPU/memoria | `kubectl -n kuber-app get pods,hpa` / `kubectl top pods -A` |
| Logs y eventos | `kubectl -n kuber-app logs deploy/kuber-backend` / `kubectl get events -A --sort-by=.lastTimestamp` |

## 7. Destruir (siempre)

```bash
./scripts/aws-down.sh
```

Desinstala Traefik (borra el balanceador) y los volúmenes, ejecuta
`terraform destroy` y lista los restos (EKS, balanceadores, volúmenes,
ECR, NAT). Todo debe salir vacío. Confirmar el gasto en el Learner Lab.

## Problemas conocidos

- **`AccessDenied` al crear EKS o el addon EBS CSI:** poner
  `enable_ebs_csi = false` en `infra/terraform/variables.tf` y, para Postgres,
  cambiar `storageClassName` por `emptyDir` en el overlay (los datos se
  pierden si el pod se recrea; las semillas se recargan solas).
- **Pods `Pending` por falta de CPU/memoria:** bajar `maxReplicas` del HPA en
  `cluster/overlays/aws/kustomization.yaml`.
- **NetworkPolicy:** en EKS solo se aplican si se activa el soporte en el CNI
  (no activado aquí). Se documenta como limitación.
- **Credenciales caducadas en el CD:** actualizar los tres secretos de GitHub.
