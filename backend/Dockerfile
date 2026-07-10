FROM python:3.11-slim

WORKDIR /app

# Install system deps: curl for healthchecks, az CLI for Grafana token refresh
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    lsb-release \
    gnupg \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root user (matches Maersk runAsUser policy)
RUN useradd -u 15000 -m appuser
USER 15000

EXPOSE 8077

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8077"]
