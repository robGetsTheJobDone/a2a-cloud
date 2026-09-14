FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

# microsandbox uses libkrun + FUSE inside the guest setup pipeline. The
# DaemonSet pod also bind-mounts /dev/kvm + /dev/fuse from the host (see
# apps/sandbox-runtime/deploy/sandbox-daemonset.yaml).
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       ca-certificates \
       fuse3 libdbus-1-3 libfuse2t64 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Both sandbox-runtime and a2a-pack source. Build context is the repo root —
# the install script does `docker build -f apps/sandbox-runtime/Dockerfile .`
COPY sdk/a2a-pack /sdk/a2a-pack
RUN pip install --no-cache-dir /sdk/a2a-pack && rm -rf /sdk

# The pinned wheel contains the native library and CLI. The URL hash is the
# PyPI-published x86_64 wheel SHA256 for microsandbox 0.3.14.
RUN pip install --no-cache-dir \
      'https://files.pythonhosted.org/packages/d1/40/af8f21cbaf4cf4d5e1cf19266f4b5f9ce87e5bbe522d7e48af201c2e9dd6/microsandbox-0.3.14-cp310-abi3-manylinux_2_28_x86_64.whl#sha256=2a77091a774e8fc0d27ecd0050913292cd234f3bb4bc37c7da7987557c7bd2bf'

COPY apps/sandbox-runtime/pyproject.toml ./
COPY apps/sandbox-runtime/sandbox_runtime ./sandbox_runtime
RUN pip install --no-cache-dir '.[service,minio]' \
  && microsandbox --version | grep -F '0.3.14'

EXPOSE 8000
CMD ["sandbox-runtime"]
