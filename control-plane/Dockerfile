FROM python:3.11-slim AS base

# git is needed for server-side scaffolding (commit_and_push_source).
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libxml2-dev \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    pkg-config \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY control_plane ./control_plane
COPY main_agent ./main_agent
COPY scripts ./scripts

RUN pip install --no-cache-dir .

# Fail the image build if security-critical runtime entrypoints were omitted
# from either the Docker context or the installed package.
RUN python -c "import control_plane.agent_ingress, control_plane.auth_exchange, control_plane.safe_http"

FROM base AS test

RUN pip install --no-cache-dir ".[dev]"

COPY . .

ARG PYTEST_EXTRA_ARGS=
RUN python -m pytest -q tests \
  ${PYTEST_EXTRA_ARGS}

FROM base AS runtime

EXPOSE 8000
CMD ["a2a-control-plane"]
