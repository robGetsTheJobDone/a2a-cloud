# a2a devbox

The image behind `a2a ssh <agent>` — a throwaway, scale-to-zero Knative box you
SSH into to work on an agent, with **node + python + a2a-pack + the agent repo**
already set up.

## Why it exists

The cluster ingress is HTTP-only (traefik/kourier, ports 80/443); there is no
raw-TCP path in. So instead of exposing SSH on port 22, the box runs `sshd` on
`127.0.0.1` and a small **WebSocket ⇄ sshd bridge** ([`devbox/bridge.py`](devbox/bridge.py))
serves the pod's HTTP port. An OpenSSH client reaches it through the existing
HTTPS ingress via a `ProxyCommand` (`a2a ssh-proxy`, shipped in the CLI):

```
ssh client ──stdio──> a2a ssh-proxy ──wss://<agent>-devbox.a2acloud.io/ssh──> bridge ──tcp──> 127.0.0.1:22 (sshd)
```

Because it's a standard `ProxyCommand`, VS Code Remote-SSH, Cursor, JetBrains
Gateway, `scp`, `rsync`, and bare `ssh` all work once `a2a ssh` writes the
`~/.ssh/config` block.

## Auth (two independent layers)

1. **Transport** — the bridge checks an access token (`A2A_DEVBOX_ACCESS_TOKEN`).
   Phase 1 is a shared token; Phase 2 swaps in Ed25519 grant verification.
2. **Session** — `sshd` only accepts the caller's ephemeral public key, injected
   at boot via `A2A_DEVBOX_AUTHORIZED_KEYS`.

Neither layer alone gets you a shell.

## Runtime env (injected by the control plane in Phase 2)

| Var | Purpose |
|---|---|
| `A2A_DEVBOX_AUTHORIZED_KEYS` | caller's ephemeral SSH public key(s) |
| `A2A_DEVBOX_ACCESS_TOKEN` | transport token the bridge checks |
| `A2A_AGENT_REPO` | agent source repo URL to clone |
| `A2A_AGENT_NAME` | clone destination dir name |
| `A2A_GITEA_TOKEN` / `A2A_GITEA_USER` | scoped, short-TTL creds for the clone |
| `A2A_DEVBOX_PORT` | bridge listen port (default 8000) |

`git`/`sudo` are available; the `dev` user has passwordless sudo (throwaway,
single-tenant box). All repo/creds are injected at boot, never baked into the
image.

## Local smoke test

```sh
docker build -t a2a-devbox:dev apps/devbox
docker run --rm -p 8022:8000 \
  -e A2A_DEVBOX_AUTHORIZED_KEYS="$(cat ~/.ssh/id_ed25519.pub)" \
  a2a-devbox:dev
# then, with a ws->stdio helper as ProxyCommand:
#   ssh -o ProxyCommand="<ws-bridge> ws://localhost:8022/ssh" dev@devbox
curl -s localhost:8022/healthz   # -> ok
```

Phase 3's `a2a ssh-proxy` is the real ProxyCommand; until then any
websocket→stdio shim works for manual testing.
