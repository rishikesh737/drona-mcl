FROM fedora:40
WORKDIR /app

# Install Python and the SysAdmin tools Drona relies on
RUN dnf install -y python3 python3-pip systemd firewalld iputils wget curl procps-ng && dnf clean all

# Set up the Python environment
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent codebase
COPY . .

# Start the FastAPI server for the Web UI streaming API
ENTRYPOINT ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
