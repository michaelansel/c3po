FROM python:3.14-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY __init__.py .
COPY server.py .
COPY agents.py .
COPY messaging.py .
COPY errors.py .
COPY auth.py .
COPY audit.py .
COPY rate_limit.py .
COPY blobs.py .

# Create the coordinator package structure
RUN mkdir -p coordinator && \
    cp __init__.py coordinator/ && \
    cp agents.py coordinator/ && \
    cp messaging.py coordinator/ && \
    cp errors.py coordinator/ && \
    cp auth.py coordinator/ && \
    cp audit.py coordinator/ && \
    cp rate_limit.py coordinator/ && \
    cp blobs.py coordinator/

# Set Python path to find the coordinator module
ENV PYTHONPATH=/app

# Default environment variables
ENV REDIS_URL=redis://localhost:6379
ENV C3PO_HOST=0.0.0.0
ENV C3PO_PORT=8420

# Create non-root user
RUN addgroup --system c3po && adduser --system --ingroup c3po c3po
USER c3po

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')"

CMD ["python", "server.py"]
