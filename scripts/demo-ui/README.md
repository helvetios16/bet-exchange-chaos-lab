# Demo visual del HPA

Interfaz HTML local para mostrar concurrencia y el autoescalado del backend en AWS:
réplicas actuales y deseadas, uso de CPU, pods y usuarios virtuales de k6.

**La interfaz y k6 se ejecutan en tu máquina; el tráfico se envía al balanceador público
que dirige a la aplicación desplegada en EKS. No levanta otra copia de la aplicación.**

## Requisitos

- Python 3 (solo biblioteca estándar; no hay paquetes que instalar).
- `kubectl` conectado al clúster EKS `chaos-lab-eks`.
- `k6` instalado para lanzar carga desde los botones. Sin k6 se puede abrir y observar,
  pero no iniciar una prueba.
- El despliegue AWS debe tener Traefik, metrics-server y el HPA del backend activos.

## Ejecutar

Desde la raíz del repositorio:

```bash
aws eks update-kubeconfig --region us-east-1 --name chaos-lab-eks
kubectl config current-context
kubectl -n kuber-app get hpa kuber-backend-hpa
k6 version
python3 scripts/demo-ui/server.py
```

Abre <http://localhost:8099>. Para usar otro puerto: `PORT=8100 python3 scripts/demo-ui/server.py`.

La terminal debe permanecer abierta mientras uses la interfaz. Para cerrar, pulsa `Ctrl+C`.

## Perfiles y seguridad operacional

- **Rampa**: hasta 300 VUs, aprox. 8 minutos; recomendado para mostrar el escalado gradual.
- **Pico**: hasta 400 VUs, aprox. 4,5 minutos; impacto más brusco.
- **Parar carga** envía una señal de terminación a k6.
- Al terminar la carga, el HPA puede tardar varios minutos (hasta unos 5 min) en bajar
  réplicas por su ventana de estabilización.

Son pruebas reales contra AWS: úsalas solo con autorización, observa coste/capacidad y
no pulses varios perfiles a la vez. El HPA de AWS tiene máximo 8 réplicas.

## ¿Cómo se conecta?

1. `kubectl` obtiene el HPA y los pods de `kuber-app`, además del hostname del servicio
   Traefik. Por ello el `kubectl` context debe apuntar a EKS.
2. La página consulta `http://localhost:8099/api/status` cada 3 segundos.
3. Al iniciar una carga, el servidor local ejecuta `tests/load/k6-load.js` con
   `BASE_URL=http://<hostname-del-LoadBalancer>` y el perfil elegido.
4. k6 genera solicitudes desde tu máquina al LoadBalancer → Traefik → backend en EKS.
   La UI vuelve a consultar HPA/pods para mostrar la reacción.

No necesita abrir puertos de entrada en AWS ni desplegar la demo UI en Kubernetes. El
servidor está enlazado a `127.0.0.1` y solo escucha localmente.

## Problemas frecuentes

- **“Sin conexión con el servidor local”**: comprueba que el servidor Python sigue activo.
- **Error de `kubectl` / HPA vacío**: revisa `aws sts get-caller-identity`, el contexto de
  `kubectl` y que los pods estén accesibles. Las credenciales temporales del Learner Lab
  caducan; actualízalas y repite `aws eks update-kubeconfig`.
- **“No se encontró k6”**: instala k6 y reinicia el servidor de la demo.
- **CPU `—` o `<unknown>`**: revisa metrics-server y `kubectl top pods -n kuber-app`.
- **La rampa no aumenta réplicas**: mira el CPU del HPA; la carga puede no superar el
  objetivo del 70 %. No uses únicamente el perfil pico para demostrar escalado sostenido.
