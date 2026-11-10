#!/usr/bin/env python3
"""Interfaz local para observar y generar carga sobre el HPA de AWS.

Ejecutar desde la raíz del repositorio:
    python3 scripts/demo-ui/server.py
Luego abrir http://localhost:8099.

La interfaz y k6 corren en esta máquina; kubectl observa el clúster EKS y k6
manda tráfico real al balanceador público de AWS. No despliega contenedores aquí.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8099"))
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
K6 = shutil.which("k6") or (r"C:\\Program Files\\k6\\k6.exe" if os.name == "nt" else None)
NS_APP = "kuber-app"
POLL_SECONDS = 3

state = {"hpa": None, "pods": [], "lb": None, "error": None, "t": 0}
history: list[tuple[float, int, int | None]] = []
load = {"proc": None, "profile": None, "start": None, "vus": 0, "log": ""}
lock = threading.Lock()
STAGES = {
    "ramp": [(60, 30), (120, 150), (120, 300), (60, 300), (120, 0)],
    "spike": [(20, 20), (10, 400), (180, 400), (60, 0)],
}


def kubectl(*args: str) -> str:
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"kubectl terminó con código {result.returncode}")
    return result.stdout.strip()


def poll() -> None:
    while True:
        try:
            lb = kubectl(
                "-n", "traefik", "get", "svc", "traefik", "-o",
                "jsonpath={.status.loadBalancer.ingress[0].hostname}",
            )
            hpa = json.loads(kubectl("-n", NS_APP, "get", "hpa", "kuber-backend-hpa", "-o", "json"))
            pod_data = json.loads(kubectl(
                "-n", NS_APP, "get", "pods", "-l", "app.kubernetes.io/name=backend", "-o", "json"
            ))

            cpu = mem = None
            for metric in hpa["status"].get("currentMetrics", []) or []:
                resource = metric.get("resource", {})
                if resource.get("name") == "cpu":
                    cpu = resource.get("current", {}).get("averageUtilization")
                elif resource.get("name") == "memory":
                    mem = resource.get("current", {}).get("averageUtilization")

            pods = []
            for item in pod_data["items"]:
                statuses = item["status"].get("containerStatuses", [])
                container = statuses[0] if statuses else {}
                pods.append({
                    "name": item["metadata"]["name"][-5:],
                    "ready": bool(container.get("ready")),
                    "phase": item["status"].get("phase"),
                    "terminating": bool(item["metadata"].get("deletionTimestamp")),
                })

            now = time.time()
            replicas = hpa["status"].get("currentReplicas", 0)
            with lock:
                state.update(
                    lb=lb or None,
                    hpa={
                        "current": replicas,
                        "desired": hpa["status"].get("desiredReplicas", 0),
                        "min": hpa["spec"]["minReplicas"],
                        "max": hpa["spec"]["maxReplicas"],
                        "cpu": cpu,
                        "mem": mem,
                    },
                    pods=pods,
                    error=None,
                    t=now,
                )
                history.append((now, replicas, cpu))
                del history[:-400]
        except Exception as exc:  # Keep the UI alive when the cluster is briefly unreachable.
            with lock:
                state["error"] = str(exc)[:200]
        time.sleep(POLL_SECONDS)


def read_k6(proc: subprocess.Popen[str]) -> None:
    if proc.stderr is None:
        return
    buf = ""
    for chunk in iter(lambda: proc.stderr.read(1), ""):
        buf += chunk
        if chunk in "\r\n":
            match = re.search(r"(\d+)/(\d+) VUs", buf)
            if match:
                with lock:
                    load["vus"] = int(match.group(1))
                    load["log"] = (load["log"] + buf)[-2000:]
            buf = ""


def start_load(profile: str) -> str:
    if profile not in STAGES:
        return "perfil no válido"
    if not K6:
        return "no se encontró k6; instala k6 y reinicia esta interfaz"
    with lock:
        proc = load["proc"]
        lb = state["lb"]
        if proc and proc.poll() is None:
            return "ya hay una carga en marcha"
        if not lb:
            return "balanceador desconocido; verifica kubectl y el servicio traefik"

        env = dict(os.environ)
        command = [
            K6, "run", "--quiet=false", "-e", f"BASE_URL=http://{lb}",
            "-e", f"PROFILE={profile}", str(ROOT / "tests" / "load" / "k6-load.js"),
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(ROOT),
            )
        except OSError as exc:
            return f"no se pudo iniciar k6: {exc}"
        load.update(proc=proc, profile=profile, start=time.time(), vus=0, log="")
    threading.Thread(target=read_k6, args=(proc,), daemon=True).start()
    return "ok"


def stop_load() -> str:
    with lock:
        proc = load["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        return "señal de parada enviada a k6"
    return "no hay carga en ejecución"


def vus_at(profile: str | None, elapsed: float) -> int:
    if profile not in STAGES:
        return 0
    previous = 0
    for duration, target in STAGES[profile]:
        if elapsed <= duration:
            return round(previous + (target - previous) * max(0, elapsed) / duration)
        elapsed -= duration
        previous = target
    return 0


def status() -> dict:
    with lock:
        proc = load["proc"]
        running = bool(proc and proc.poll() is None)
        started = load["start"]
        elapsed = time.time() - started if started and running else 0
        return {
            "hpa": state["hpa"],
            "pods": list(state["pods"]),
            "error": state["error"],
            "lb": state["lb"],
            "history": [(round(t), r, c) for t, r, c in history[-200:]],
            "load": {
                "log": load["log"][-300:],
                "running": running,
                "profile": load["profile"],
                "vus": vus_at(load["profile"], elapsed) if running else 0,
                "elapsed": int(elapsed),
            },
        }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str | bytes, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send(200, json.dumps(status()))
        elif path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/load/start":
            profile = parse_qs(parsed.query).get("profile", [""])[0]
            result = start_load(profile)
            self._send(200 if result == "ok" else 400, json.dumps({"result": result}))
        elif parsed.path == "/api/load/stop":
            self._send(200, json.dumps({"result": stop_load()}))
        else:
            self._send(404, "{}")

    def log_message(self, *_args: object) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=poll, daemon=True).start()
    print(f"Demo HPA en http://localhost:{PORT} (k6: {K6 or 'no encontrado'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
