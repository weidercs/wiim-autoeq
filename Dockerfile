FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV PYTHONUNBUFFERED=1 \
    WIIM_DATA_DIR=/data

# Default: the wiim-eq service (UI + /api/v1 + /mcp) on 8000.
# Discovery sidecar:  python3 src/wiim_discovery.py   (needs network_mode: host)
# Plain web UI only:  python3 src/wiim_autoeq_web.py --host 0.0.0.0 --port 5173
EXPOSE 8000
USER 1000:1000
CMD ["python3", "src/wiim_eq_service.py"]
