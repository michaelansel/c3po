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

# Create the coordinator package structure
RUN mkdir -p coordinator && \
    cp __init__.py coordinator/ && \
    cp agents.py coordinator/ && \
    cp messaging.py coordinator/ && \
    cp errors.py coordinator/

# Set Python path to find the coordinator module
ENV PYTHONPATH=/app

# Default environment variables
ENV REDIS_URL=redis://localhost:6379
ENV C3PO_HOST=0.0.0.0
ENV C3PO_PORT=8420

EXPOSE 8420

CMD ["python", "server.py"]
