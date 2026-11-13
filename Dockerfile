FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV DATA_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    CORRIDA_DIARIA=06:15
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/salud')" || exit 1
CMD ["python", "app.py"]
