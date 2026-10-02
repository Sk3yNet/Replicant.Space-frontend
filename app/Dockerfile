FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rsweb ./rsweb

RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# One worker: the event ingester and rate limiter live in-process.
# --timeout-graceful-shutdown: open SSE streams would otherwise hold shutdown until Docker kills the container (10 s),
# so the app never records a clean stop.
CMD ["uvicorn", "rsweb.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "4"]
