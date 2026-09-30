# =============================================================================
# Imagen del generador de carga.
# Sin dependencias de sistema: aiohttp viene con wheels precompilados.
# =============================================================================
FROM python:3.12-slim

RUN groupadd -g 10001 kuber \
 && useradd -u 10001 -g 10001 -m -s /sbin/nologin -M kuber

# Versiones fijadas para que el generador mida lo mismo en cada ejecución
RUN pip install --no-cache-dir aiohttp==3.11.11

WORKDIR /load
COPY load.py .

USER 10001:10001

# PID 1 con exec form para que SIGTERM llegue al proceso de Python y el
# generador pueda terminar las requests en vuelo en vez de perderlas.
CMD ["python", "/load/load.py"]
