FROM python:3.12-slim

# Install system utilities & kubectl for read-only CLI inspections
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source and manifests
COPY src/ ./src/
COPY k8s/ ./k8s/

# Set Python path
ENV PYTHONPATH=/app/src

EXPOSE 8000

# Run FastAPI via uvicorn
CMD ["uvicorn", "kubeheal.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
