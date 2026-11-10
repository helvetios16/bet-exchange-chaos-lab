# Demo UI: concurrencia y autoescalado del HPA

La demo UI es una página HTML servida localmente que permite **visualizar y provocar**
el escalado del backend en AWS. Muestra réplicas actuales/deseadas, CPU, pods, usuarios
virtuales k6 y una gráfica de la prueba. Los botones ejecutan carga real contra AWS.

> No es el `dashboard.html` de arquitectura que vive en la raíz del repo, ni el dashboard
de Grafana. Esta es la interfaz interactiva específica para la demo de concurrencia/HPA:
[`scripts/demo-ui/index.html`](../scripts/demo-ui/index.html).

## Recorrido de la conexión

```text
Navegador
  └─ http://localhost:8099 (scripts/demo-ui/server.py)
       ├─ kubectl → API de EKS: lee HPA, pods y hostname del servicio Traefik
       └─ botón de carga → k6 local → LoadBalancer AWS
                                  └─ Traefik → backend en EKS
```

La aplicación, la base de datos y los pods **siguen ejecutándose en AWS**. Solo el panel
y el proceso generador k6 corren en la laptop. No hace falta exponer ni desplegar el
servidor de la demo dentro del clúster.

## Requisitos

- Python 3; `server.py` usa solo la biblioteca estándar.
- AWS CLI con una sesión activa y permisos de acceso al clúster EKS.
- `kubectl` configurado para `chaos-lab-eks`.
- k6 instalado para iniciar carga desde la interfaz. Sin k6, la página todavía sirve
  para mostrar telemetría del HPA, pero no podrá lanzar pruebas.
- Despliegue AWS activo: Traefik/LoadBalancer, backend, HPA y metrics-server.

## Iniciar la demo en AWS

Desde la raíz del repositorio, en Terminal:

```bash
aws sts get-caller-identity
aws eks update-kubeconfig --region us-east-1 --name chaos-lab-eks
kubectl config current-context
kubectl -n kuber-app get hpa kuber-backend-hpa
kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend
k6 version
python3 scripts/demo-ui/server.py
```

`kubectl config current-context` debe mostrar el contexto del clúster EKS. Si los comandos
`kubectl get hpa` y `kubectl get pods` funcionan y k6 está instalado, abre
<http://localhost:8099>. Mantén la terminal abierta; `Ctrl+C` cierra la UI.

Para usar un puerto distinto:

```bash
PORT=8100 python3 scripts/demo-ui/server.py
```

Abre entonces <http://localhost:8100>.

## Mostrar el escalado

1. Enseña el estado inicial: réplicas, CPU, pods y balanceador.
2. Pulsa **Lanzar rampa** para una subida gradual (hasta 300 VUs, aprox. 8 min).
   Es el perfil recomendado para mostrar cómo el HPA escala de 2 hacia más réplicas.
3. Observa las réplicas/pods verdes y la CPU en la interfaz; el sondeo se actualiza cada
   3 segundos. Puedes contrastar en otra terminal con:

   ```bash
   kubectl -n kuber-app get hpa kuber-backend-hpa --watch
   kubectl -n kuber-app get pods -l app.kubernetes.io/name=backend --watch
   ```

4. Usa **Parar carga** para detener k6. El HPA puede tardar hasta unos 5 minutos en
   reducir réplicas por su ventana de estabilización.
5. El perfil **Pico** puede llegar a 400 VUs durante unos 3 minutos; su subida brusca es
   más agresiva y puede no ser tan clara para demostrar escalado gradual.

El máximo del HPA AWS es 8 réplicas. Una carga que no mantenga suficiente CPU puede no
hacerlo escalar: comprueba el porcentaje de CPU, `kubectl top pods -n kuber-app` y los
`TARGETS` del HPA. Si aparece `<unknown>`, revisa metrics-server.

## Coste y precauciones

Los VUs generan solicitudes reales contra el balanceador AWS y pueden consumir recursos
(o afectar la demo compartida). Avisa al equipo antes de iniciar, ejecuta un solo perfil,
observa el gasto del Learner Lab y detén la prueba al terminar. No dejes el clúster
encendido cuando ya no se necesite.

## Interfaz distinta: Grafana

Para series y métricas de Prometheus (dashboards persistentes), usa Grafana en vez de la
Demo UI. Sigue [`docs/RUNBOOK-AWS.md`](RUNBOOK-AWS.md#4-desplegar) y abre el dashboard
**Chaos Lab - Backend y HPA**. La Demo UI es especialmente cómoda para accionar k6 y
mostrar la respuesta del HPA en vivo.

## Código

- HTML: [`scripts/demo-ui/index.html`](../scripts/demo-ui/index.html)
- Servidor local y API de estado/control: [`scripts/demo-ui/server.py`](../scripts/demo-ui/server.py)
- Instrucciones junto al código: [`scripts/demo-ui/README.md`](../scripts/demo-ui/README.md)
- Perfil de carga real: [`tests/load/k6-load.js`](../tests/load/k6-load.js)
- Manifiesto/HPA y ajuste AWS (máximo 8): [`cluster/overlays/aws/kustomization.yaml`](../cluster/overlays/aws/kustomization.yaml)
