"""Configuración común de pytest.

Pone app/backend en sys.path para poder hacer `import app` igual que en la
imagen Docker (donde el código vive en /app).
"""
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "app" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
