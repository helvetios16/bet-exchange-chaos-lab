"""Configuración común de pytest.

Pone app/backend en sys.path para poder hacer `import app` igual que en la
imagen Docker (donde el código vive en /app).
"""
import asyncio
import pathlib
import sys

# psycopg en modo asíncrono no funciona con el ProactorEventLoop, que es el
# bucle por defecto en Windows. En Linux (CI, contenedor) no hace falta.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "app" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
