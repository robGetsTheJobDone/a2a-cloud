# Base image for A2A agents. User agents `FROM` this so the SDK is
# pre-installed and rebuilds are fast.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables nftables \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sdk

COPY pyproject.toml LICENSE README.md ./
COPY a2a_pack ./a2a_pack

RUN pip install --no-cache-dir .

# Sanity check
RUN python -c "import a2a_pack; print('a2a-pack', a2a_pack.__version__, 'ok')"

WORKDIR /app
