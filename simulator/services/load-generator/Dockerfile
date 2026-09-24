FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27,<1.0"
COPY simulator/services/load-generator/generator.py ./generator.py
CMD ["python", "generator.py"]
