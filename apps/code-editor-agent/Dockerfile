# Build the base first: docker build -t a2a-pack-base:latest -f sdk/a2a-pack/Dockerfile sdk/a2a-pack
ARG BASE_IMAGE=a2a-pack-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      https://github.com/colbymchenry/codegraph/releases/download/v0.9.4/codegraph-linux-x64.tar.gz \
      -o /tmp/codegraph.tar.gz \
    && echo 'af4dfe25c17868d2260cec243e702de088738ba8bf379962bed8296362eb0c8a  /tmp/codegraph.tar.gz' | sha256sum -c - \
    && mkdir -p /opt/codegraph \
    && tar -xzf /tmp/codegraph.tar.gz -C /opt/codegraph --strip-components=1 \
    && rm -f /tmp/codegraph.tar.gz \
    && ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph \
    && codegraph --version

COPY . .

ENV A2A_ENTRYPOINT=agent:CodeEditorAgent
ENV PORT=8000
EXPOSE 8000

CMD a2a run --entrypoint "$A2A_ENTRYPOINT" --host 0.0.0.0 --port 8000
