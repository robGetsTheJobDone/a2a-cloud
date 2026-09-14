FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sdk
COPY pyproject.toml LICENSE README.md ./
COPY a2a_pack ./a2a_pack
RUN python3 -m venv /opt/a2a-sidecar \
    && /opt/a2a-sidecar/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/a2a-sidecar/bin/pip install --no-cache-dir .

COPY docker/sidecar/a2a-sidecar-build /usr/local/bin/a2a-sidecar-build
RUN chmod +x /usr/local/bin/a2a-sidecar-build \
    && /opt/a2a-sidecar/bin/a2a --help >/dev/null

ENV PATH="/opt/a2a-sidecar/bin:${PATH}"
WORKDIR /app

