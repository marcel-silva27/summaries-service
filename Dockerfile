FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home app
COPY app ./app
USER app
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://localhost:8000/health', timeout=2)"]
# Entrega: sin --reload ni volumen de código. Para desarrollo usar docker-compose.dev.yaml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
