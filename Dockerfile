# Drona-MCL Dockerfile
# Builds a minimal Python image containing the Drona agent.
# The Ollama model is NOT embedded here; it lives in the ollama service.

FROM python:3.11-slim

# Install system dependencies needed by the tools
# (ping, ss, journalctl, curl, df, free are expected on the host via /proc
#  when running with --privileged or network_mode: host)
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    iputils-ping \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache efficiency)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY core/       ./core/
COPY tools/      ./tools/
COPY config/     ./config/
COPY tests/      ./tests/
COPY main.py     .

# Create ai_workspace (bind-mounted from host in compose, but needed standalone)
RUN mkdir -p ai_workspace

# Non-root user for security
RUN useradd --create-home --shell /bin/bash drona
USER drona

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
