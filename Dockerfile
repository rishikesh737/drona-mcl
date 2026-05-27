FROM fedora:40
WORKDIR /app

# Install Python and the SysAdmin tools Drona relies on
RUN dnf install -y python3 python3-pip systemd firewalld iputils wget curl procps-ng && dnf clean all

# Set up the Python environment
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent codebase
COPY . .

# Keep the container running infinitely in the background so we can dispatch tasks to it
ENTRYPOINT ["tail", "-f", "/dev/null"]
