FROM python:3.11-slim
WORKDIR /app
COPY simulator/services/common/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY simulator/services/common ./simulator/services/common
COPY simulator/services/checkout-service/app.py ./app.py
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${SERVICE_PORT:-8001}"]
