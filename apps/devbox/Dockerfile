# a2a devbox: a throwaway, scale-to-zero box you SSH into (over WSS through the
# HTTP ingress). Built on the node sidecar image so node + python + a2a-pack are
# already present; we add git, sshd, and the WS<->sshd bridge on top.
# Build the base first: docker build -t a2a-sidecar-node:latest -f sdk/a2a-pack/docker/sidecar/node.Dockerfile sdk/a2a-pack
ARG BASE_IMAGE=a2a-sidecar-node:latest
FROM ${BASE_IMAGE}

ENV DEVBOX_USER=dev \
    A2A_DEVBOX_USER=dev \
    A2A_DEVBOX_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git openssh-server tini curl ca-certificates bash sudo rsync \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd \
    && useradd --create-home --shell /bin/bash "${DEVBOX_USER}" \
    && echo "${DEVBOX_USER} ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/devbox \
    && chmod 440 /etc/sudoers.d/devbox

# Login shells (incl. VS Code Remote-SSH) inherit node + a2a on PATH via PAM's
# /etc/environment (sourced for non-login ssh sessions too).
RUN printf 'PATH="/opt/a2a-sidecar/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n' \
      >/etc/environment

# The sidecar base carries a2a-pack but can lag releases — pin the minimum the
# boxes must speak; each release train bumps this, which busts the layer cache.
RUN /opt/a2a-sidecar/bin/pip install --no-cache-dir -U 'a2a-pack>=0.1.106'

# Bridge: install into the venv that already carries a2a-pack.
COPY pyproject.toml README.md /opt/devbox/
COPY devbox /opt/devbox/devbox
RUN /opt/a2a-sidecar/bin/pip install --no-cache-dir /opt/devbox

# The dev user's PATH points at this venv (see /etc/environment above), so
# `a2a dev` syncs run pip as `dev` against /opt/a2a-sidecar. The venv is built
# as root, so hand it to `dev` here — otherwise on-box dependency installs fail
# with "Permission denied: /opt/a2a-sidecar/.../site-packages/...". Throwaway
# box; dev already has passwordless sudo, so this widens nothing meaningful.
RUN chown -R "${DEVBOX_USER}:${DEVBOX_USER}" /opt/a2a-sidecar

COPY sshd_config /etc/ssh/sshd_config.d/devbox.conf
COPY entrypoint.sh /usr/local/bin/devbox-entrypoint
RUN chmod +x /usr/local/bin/devbox-entrypoint

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/devbox-entrypoint"]
