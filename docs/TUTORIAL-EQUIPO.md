# Tutorial paso a paso para el equipo

Sirve para que cualquier integrante parta de cero y haga funcionar el proyecto.
Está en el orden en que conviene hacerlo. Cada parte indica **qué se verificó
realmente** y qué todavía no.

| Parte | Qué logras | Cuesta dinero | Estado de verificación |
|---|---|---|---|
| A | Instalar herramientas | No | — |
| B | Bajar el código | No | Verificado |
| C | Correr los tests en tu PC | No | Verificado (58 tests en verde) |
| D | Entender el flujo de trabajo en GitHub | No | Parcial (el CI aún no corrió en GitHub) |
| E | Crear la infraestructura en AWS | **Sí (~0,25 USD/h)** | **Sin ejecutar todavía** (Terraform solo validado) |
| F | Desplegar con el pipeline | Sí | Sin ejecutar |
| G | Sacar las evidencias para la demo | Sí | Sin ejecutar |
| H | Destruir todo | — | Sin ejecutar |
| I | Problemas frecuentes | — | Hechos reales de esta sesión |

> **Regla de oro:** solo **una persona** crea el clúster en AWS (la dueña del
> laboratorio con crédito). Los demás trabajan con las partes A–D en su PC.

---

## Parte A. Instalar las herramientas

Estos comandos son para **Windows 11** (PowerShell). Después de instalar,
**cierra y abre una terminal nueva** para que reconozca los programas.

```powershell
winget install Git.Git
winget install Python.Python.3.12
winget install Docker.DockerDesktop
winget install Amazon.AWSCLI
winget install Hashicorp.Terraform
winget install Kubernetes.kubectl
winget install Helm.Helm
winget install k6.k6
winget install GitHub.cli
```

Comprueba que todo responde:

```powershell
git --version
python --version        # debe ser 3.12.x
docker --version
aws --version
terraform version
kubectl version --client
helm version
k6 version
```

Los scripts `scripts/*.sh` son de bash. En Windows ejecútalos desde
**Git Bash** (viene con Git), no desde PowerShell.

En macOS o Linux usa `brew` o el gestor de paquetes de tu sistema; los
programas son los mismos.

---

## Parte B. Bajar el código

El trabajo está en la rama `feat/tests-and-ci` del fork de Sergio.

```bash
git clone https://github.com/sergioandante/bet-exchange-chaos-lab-sergio.git
cd bet-exchange-chaos-lab-sergio
git switch feat/tests-and-ci
```

Para ver la guía general, abre `README.md` (sección "Guía del equipo").

---

## Parte C. Probar el código en tu PC (gratis)

Necesitas **Python 3.12** y **Docker Desktop abierto** (solo para los tests de
integración).

### C.1 Entorno de Python

```bash
python -m venv .venv
source .venv/Scripts/activate        # Git Bash en Windows
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Si `pip` falla al instalar `psycopg-binary==3.2.3`, estás usando Python 3.13 o
superior, que no tiene esa versión compilada. Usa Python 3.12.

### C.2 Análisis estático y tests unitarios

```bash
ruff check app tests          # debe decir: All checks passed!
pytest tests/unit             # 41 tests; no necesitan base de datos
```

### C.3 Tests de integración (con PostgreSQL real)

Levanta un Postgres temporal en el puerto 55432 y ejecuta los tests:

```bash
docker run -d --name pg-test -e POSTGRES_USER=kuber -e POSTGRES_PASSWORD=kuber \
  -e POSTGRES_DB=kuber_test -p 55432:5432 postgres:16

REQUIRE_DB=1 TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 pytest tests/integration

docker rm -f pg-test
```

Resultado esperado: **17 passed**. Los tests comprueban sobre todo que
`drift = 0` (el saldo coincide con el ledger) tras apuestas y liquidaciones.

Si no pones `REQUIRE_DB=1` y no hay base de datos, los tests de integración se
**saltan** en vez de fallar; el CI sí tiene `REQUIRE_DB=1`.

### C.4 (Opcional) Ejecutar toda la aplicación en tu PC con `kind`

Es el flujo original del laboratorio, sin AWS. Requiere Docker, `kind`,
`kubectl`, `helm` y unos 8 GB de RAM libres. Sigue
[`docs/COMO-PROBAR.md`](COMO-PROBAR.md). No lo hemos reverificado en esta
sesión.

---

## Parte D. Flujo de trabajo en GitHub

1. Pide a Sergio que te agregue como colaborador (*Settings → Collaborators*),
   o haz tu propio fork.
2. Crea una rama por tarea:
   ```bash
   git switch -c feat/mi-tarea
   ```
3. Haz commits pequeños y sube:
   ```bash
   git add -A && git commit -m "Describe qué cambiaste"
   git push -u origin feat/mi-tarea
   ```
4. En GitHub abre un **Pull Request** hacia `main`. Comprueba que la base sea el
   repo correcto (el del fork de Sergio, no el de helvetios16).
5. **Habilitar el CI la primera vez:** en la pestaña *Actions* del fork, si ves
   el botón verde *"I understand my workflows, go ahead and enable them"*,
   púlsalo.
6. El CI ejecuta ruff, los tests (con un Postgres de servicio) y construye las
   imágenes. **Solo se fusiona con el CI en verde.**

### Demostrar que un test roto bloquea el pipeline (lo pide la spec)

1. Crea una rama y rompe un test a propósito (por ejemplo, cambia un valor
   esperado en `tests/unit/test_backend_unit.py`).
2. Abre un PR. El job `test` queda en rojo y el job `build` **no se ejecuta**.
3. Haz captura. Después corrige y confirma que vuelve a verde.

---

## Parte E. Crear la infraestructura en AWS (solo la persona dueña del lab)

> ⚠️ **Antes de empezar:** mira el presupuesto del lab (*Used $X of $Y* arriba a
> la izquierda). El entorno cuesta ≈ **0,25 USD por hora**; un clúster olvidado
> gasta ≈ 6 USD al día.

### E.1 Iniciar el lab y copiar las credenciales

1. Entra al curso **AWS Academy Learner Lab** → *Launch AWS Academy Learner Lab*.
2. Pulsa **Start Lab** y espera a que el círculo junto a "AWS" esté **verde**.
3. Pulsa **AWS Details** → junto a *AWS CLI* pulsa **Show**.
4. Copia el bloque completo (con `[default]`):
   ```
   [default]
   aws_access_key_id=ASIA...
   aws_secret_access_key=...
   aws_session_token=...
   ```
5. Pégalo en este archivo (créalo si no existe):
   - Windows: `C:\Users\TU_USUARIO\.aws\credentials`
   - macOS/Linux: `~/.aws/credentials`

   **El archivo se llama exactamente `credentials`, sin `.txt`.** El Bloc de
   notas suele añadir `.txt` y entonces la CLI no lo encuentra. Compruébalo con
   `Get-ChildItem $HOME\.aws` en PowerShell.
6. Verifica:
   ```bash
   aws sts get-caller-identity
   ```
   Debe mostrar el rol `voclabs`. Si da `ExpiredToken`, copia de nuevo el bloque.

> Estas credenciales **caducan** (~4 h). Al terminar el tiempo o al detener el
> lab, AWS bloquea todo con una política de denegación (`voc-cancel-cred`) y
> hay que volver a pulsar *Start Lab*. **Nunca** las subas a git ni las pegues
> en un chat.

### E.2 Planificar la infraestructura (no crea nada)

```bash
cd infra/terraform
terraform init
terraform plan
```

Debe terminar con `Plan: N to add, 0 to change, 0 to destroy`. Si falla con
`AccessDenied`, el lab no está iniciado o la sesión caducó (ver E.1).

### E.3 Crear la infraestructura (esto sí cuesta)

Desde la raíz del repo, en **Git Bash**:

```bash
./scripts/aws-up.sh
```

Crea el clúster EKS, 3 nodos `t3.medium`, el driver de volúmenes y tres
repositorios ECR. Tarda **12–18 minutos**. Al terminar imprime el
`ECR_REGISTRY` y deja `kubectl` apuntando al clúster:

```bash
kubectl get nodes        # deben verse 3 nodos en estado Ready
```

Si falla con `AccessDenied` al crear el driver EBS CSI, consulta
[`docs/RUNBOOK-AWS.md`](RUNBOOK-AWS.md) (sección "Problemas conocidos").

---

## Parte F. Desplegar la aplicación

### F.1 Secretos del repositorio en GitHub

En el fork: *Settings → Secrets and variables → Actions → New repository
secret*. Crea estos tres, con los valores del bloque de credenciales (E.1):

| Nombre | Valor |
|---|---|
| `AWS_ACCESS_KEY_ID` | el de `aws_access_key_id` |
| `AWS_SECRET_ACCESS_KEY` | el de `aws_secret_access_key` |
| `AWS_SESSION_TOKEN` | el de `aws_session_token` |

> **Hay que actualizarlos cada vez que reinicias el lab**, porque cambian. Y
> deben ser **de la misma cuenta que creó el clúster**.

### F.2 Ejecutar el pipeline de despliegue (CD)

El workflow `CD` se activa solo cuando el CI termina en verde sobre `main`. Por
eso:

1. Fusiona el PR a `main` (con el CI en verde).
2. Ve a *Actions → CD*. Si no se lanzó solo, pulsa **Run workflow**.
   (El botón y el disparo automático solo existen cuando `cd.yml` ya está en
   `main`.)
3. El pipeline hace: construir imágenes → subirlas a ECR (etiqueta = SHA del
   commit) → desplegar con `scripts/deploy-aws.sh` → *smoke test* → si algo
   falla, `rollout undo`.

### F.3 Comprobar el resultado

```bash
kubectl -n kuber-app get pods,hpa
kubectl -n traefik get svc traefik      # columna EXTERNAL-IP = dirección pública
./scripts/smoke-test.sh                 # comprueba /health, /events y /
```

Abre `http://<EXTERNAL-IP>` en el navegador. Para comprobar qué versión corre:

```bash
curl http://<EXTERNAL-IP>/api/v1/version
```

Devuelve el SHA del commit desplegado.

### F.4 (Alternativa) Desplegar a mano sin pipeline

Solo si el CD no está listo. Necesita las imágenes ya en ECR:

```bash
IMAGE_TAG=<sha> ECR_REGISTRY=<cuenta>.dkr.ecr.us-east-1.amazonaws.com \
  ./scripts/deploy-aws.sh
```

---

## Parte G. Evidencias para la demo (la spec pide ejecuciones reales)

### G.1 Recuperación ante fallos (requisito 4.7)

```bash
./scripts/demo-chaos.sh
```

Mata un pod del backend y el de Postgres con tráfico en marcha y comprueba que
`drift` sigue en 0. Guarda la salida de la terminal.

### G.2 Prueba de carga y autoscaling (requisito 4.8)

Terminal 1 (observa el HPA y guarda un CSV en `reports/`):

```bash
./scripts/demo-hpa.sh
```

Terminal 2 (carga):

```bash
k6 run -e BASE_URL=http://<EXTERNAL-IP> tests/load/k6-load.js
# pico brusco, como el inicio de un partido:
k6 run -e BASE_URL=http://<EXTERNAL-IP> -e PROFILE=spike tests/load/k6-load.js
```

Qué esperar: el HPA sube las réplicas del backend de 2 hasta 8 al subir la CPU
(tarda 1–2 minutos en reaccionar) y baja despacio al terminar (ventana de 5
minutos).

### G.3 Monitorización (requisito 4.9)

```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

Abre http://localhost:3000, usuario `admin`. La contraseña se genera al
desplegar:

```bash
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
```

Abre el dashboard **Chaos Lab - Backend y HPA** y tómale capturas **mientras
corre la carga**: réplicas reales vs deseadas, CPU, memoria, latencia p95/p99,
reinicios.

---

## Parte H. Destruir todo (obligatorio al terminar)

```bash
./scripts/aws-down.sh
```

Desinstala Traefik (así se borra el balanceador), borra los volúmenes, ejecuta
`terraform destroy` y lista los restos. **Todo debe salir vacío**: clústeres
EKS, balanceadores, volúmenes libres, repositorios ECR. Después confirma en el
lab que el gasto no sigue subiendo.

---

## Parte I. Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `aws : el término 'aws' no se reconoce` | AWS CLI no instalada o terminal vieja | `winget install Amazon.AWSCLI` y abrir terminal nueva |
| `Unable to locate credentials` | El archivo se llama `credentials.txt` | Renombrarlo a `credentials` (sin extensión) |
| `ExpiredToken` / `InvalidClientTokenId` | Credenciales del lab caducadas | *Start Lab* de nuevo y copiar el bloque nuevo |
| `AccessDenied ... explicit deny ... voc-cancel-cred` | La sesión del lab terminó | *Start Lab* de nuevo; revisar que no se agotó el presupuesto |
| `psycopg-binary==3.2.3` no se instala | Python 3.13+ | Usar Python 3.12 |
| Los tests de integración se saltan | No hay PostgreSQL | Levantar el contenedor de C.3 y usar `REQUIRE_DB=1` |
| `Psycopg cannot use the 'ProactorEventLoop'` | Windows con el bucle de eventos por defecto | Ya corregido en `tests/conftest.py`; actualizar la rama |
| Docker Desktop no arranca: `...dockerInference: The file cannot be accessed` | Socket huérfano de un cierre incorrecto | Cerrar Docker, renombrar la carpeta `%LOCALAPPDATA%\Docker\run` (por ejemplo a `run.bak`) y abrirlo de nuevo |
| Docker arranca muy lento o se cuelga | Poca RAM libre | Cerrar otras aplicaciones; `wsl --shutdown` y reiniciar Docker |
| `terraform apply` falla con `AccessDenied` en el addon EBS CSI | Permisos del lab | Ver "Problemas conocidos" de `docs/RUNBOOK-AWS.md` |
| Pods en `Pending` | Faltan CPU o memoria en los 3 nodos | Bajar `maxReplicas` en `cluster/overlays/aws/kustomization.yaml` |
| El CD falla al autenticarse en AWS | Secretos de GitHub caducados | Actualizar los 3 secretos (F.1) |
| El balanceador no responde justo tras crearse | El DNS tarda unos minutos | Esperar 2–5 minutos (`smoke-test.sh` reintenta solo) |
| La carga de k6 no hace subir las réplicas | Límites de tasa del Ingress | El overlay AWS ya los sube; revisar que se aplicó `cluster/overlays/aws` |

---

## Checklist para el día de la demo

- [ ] Lab iniciado, credenciales válidas y presupuesto revisado.
- [ ] Infraestructura creada (`aws-up.sh`) **antes** de la presentación (tarda 15 min).
- [ ] Secretos de GitHub actualizados y CD probado una vez completo.
- [ ] `kubectl get nodes` muestra 3 nodos Ready; `smoke-test.sh` en verde.
- [ ] Grafana abierto con el dashboard cargado.
- [ ] Cambio listo para mostrar (por ejemplo, algo visible en `/api/v1/version`).
- [ ] Terminal 1 con `demo-hpa.sh`, Terminal 2 con k6 preparado.
- [ ] Después: `aws-down.sh` y comprobar que no queda nada.
