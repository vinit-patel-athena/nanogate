FROM python:3.12-slim

WORKDIR /app

# Install system deps for building packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl build-essential && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Copy and install nanogate (pulls nanobot as a dependency)
COPY pyproject.toml .
COPY gateway/ gateway/
COPY agent/ agent/

RUN pip install --no-cache-dir .

# Default config directory
RUN mkdir -p /root/.nanobot

EXPOSE 8765

CMD ["python", "-m", "agent.server"]
