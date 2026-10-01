# Benchmark image for the stack ADR spike. Linux, like the production VPS (D8).
FROM python:3.13-slim
WORKDIR /bench
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
ENTRYPOINT ["python", "bench.py"]
