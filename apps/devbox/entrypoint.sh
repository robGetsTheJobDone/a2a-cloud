#!/usr/bin/env bash
# Devbox workload (PID 1 is tini). Prep the box, start sshd on localhost, then
# run the WS<->sshd bridge in the foreground as the container's main process.
set -euo pipefail

USER_NAME="${A2A_DEVBOX_USER:-dev}"
HOME_DIR="/home/${USER_NAME}"

# Stable per-box host key injected by the control plane, so clients don't see
# host-key-changed errors after an image roll or scale-from-zero. ssh-keygen -A
# then only fills in whatever key types are still missing.
if [ -n "${A2A_DEVBOX_HOST_KEY_ED25519:-}" ]; then
  printf '%s' "${A2A_DEVBOX_HOST_KEY_ED25519}" >/etc/ssh/ssh_host_ed25519_key
  chmod 600 /etc/ssh/ssh_host_ed25519_key
  ssh-keygen -y -f /etc/ssh/ssh_host_ed25519_key >/etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null || true
fi
ssh-keygen -A >/dev/null 2>&1 || true

# authorized_keys = the caller's ephemeral public key, injected by the control
# plane (newline-separated). Without it, no one can log in.
install -d -m 700 -o "${USER_NAME}" -g "${USER_NAME}" "${HOME_DIR}/.ssh"
if [ -n "${A2A_DEVBOX_AUTHORIZED_KEYS:-}" ]; then
  printf '%s\n' "${A2A_DEVBOX_AUTHORIZED_KEYS}" >"${HOME_DIR}/.ssh/authorized_keys"
  chmod 600 "${HOME_DIR}/.ssh/authorized_keys"
  chown "${USER_NAME}:${USER_NAME}" "${HOME_DIR}/.ssh/authorized_keys"
else
  echo "devbox: WARNING no A2A_DEVBOX_AUTHORIZED_KEYS injected; ssh login disabled" >&2
fi

# CLI login for the box owner, injected per session by the control plane, so
# `a2a` inside the box is already authenticated as the caller.
if [ -n "${A2A_CREDENTIALS_JSON:-}" ]; then
  install -d -m 700 -o "${USER_NAME}" -g "${USER_NAME}" "${HOME_DIR}/.a2a"
  printf '%s' "${A2A_CREDENTIALS_JSON}" >"${HOME_DIR}/.a2a/credentials.json"
  chmod 600 "${HOME_DIR}/.a2a/credentials.json"
  chown "${USER_NAME}:${USER_NAME}" "${HOME_DIR}/.a2a/credentials.json"
fi

# Best-effort clone of the agent repo so it's ready in the shell. Optional, so
# the image runs standalone (local test) when these env vars are unset.
if [ -n "${A2A_AGENT_REPO:-}" ]; then
  dest="${HOME_DIR}/${A2A_AGENT_NAME:-agent}"
  if [ ! -e "${dest}/.git" ]; then
    url="${A2A_AGENT_REPO}"
    if [ -n "${A2A_GITEA_TOKEN:-}" ] && [[ "${url}" != *"@"* ]]; then
      url="${url/https:\/\//https://${A2A_GITEA_USER:-oauth2}:${A2A_GITEA_TOKEN}@}"
    fi
    su - "${USER_NAME}" -c "git clone --depth 50 '${url}' '${dest}'" \
      || echo "devbox: repo clone failed (continuing)" >&2
  fi
fi

# sshd on localhost only; the bridge fronts it.
/usr/sbin/sshd -D -e &
SSHD_PID=$!
trap 'kill "${SSHD_PID}" 2>/dev/null || true' EXIT

exec /opt/a2a-sidecar/bin/python -m devbox.bridge
