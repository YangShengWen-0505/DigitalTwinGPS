FROM python:3.10-slim

WORKDIR /workspace

RUN useradd --create-home --uid 10001 appuser

COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY . .
RUN mkdir -p /workspace/data /workspace/logs \
    && chown -R appuser:appuser /workspace
USER appuser

CMD ["python", "run_server.py"]
