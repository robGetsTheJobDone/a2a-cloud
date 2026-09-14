# Cloud dev with `a2a dev`

`a2a dev` runs your agent **in the cloud while you edit locally**. It syncs your
project to the agent's scale-to-zero [dev box](/concepts/dev-boxes), runs the
agent's hot-reloading server there, and serves it on a public URL. Save a file
and the change is live in seconds — on real cloud infrastructure, not your
laptop.

```bash
cd my-agent/
a2a dev
```

You get a URL like `https://my-agent-devbox.a2acloud.io/` — a live, shareable
endpoint for the agent — and a file watcher that pushes every save up to the
box, which reloads on the change.

## Why run dev in the cloud

The inner loop stays local — your editor, your keystrokes — but the agent runs
where it deploys:

- **Parity** — the same base image, the same runtime as production. No "works on
  my machine": if it runs in `a2a dev`, it runs deployed.
- **A public URL for free** — point a webhook at it, share it in a PR, demo it to
  a teammate, or connect it to a client. No tunneling tool to run.
- **Nothing to install locally** — no Docker, no per-agent Python environment.
  The box already has the toolchain.

## The loop

```text
a2a dev
 → wakes the agent's dev box (cold start takes a few seconds)
 → syncs your project to the box
 → runs the agent's dev server there (hot reload)
 → prints https://<agent>-devbox.a2acloud.io/   ← public, shareable
 → watches local files; every save syncs up and the box reloads
```

Edit `agent.py`, hit save, and within a couple of seconds the running agent on
the box has your change. The box's dev server watches the synced files, so the
reload is automatic — the CLI only keeps the files in sync.

## Run on your machine instead

When you're offline, air-gapped, or just want the agent in your own process, use
`--local`:

```bash
a2a dev --local              # run on this machine (the classic local dev server)
a2a dev --local --host-runtime  # run directly in your Python, no container
```

`a2a dev --local` is the old behavior: it serves the agent at
`http://127.0.0.1:8000` with hot reload, using `.env.local` and a local
workspace.

## How it works

The dev box exposes a single public origin over the platform's HTTPS ingress.
A small bridge on the box fronts two things on that origin:

- `/ssh` — the WebSocket tunnel that carries file sync and shell access (the same
  channel [`a2a ssh`](/concepts/dev-boxes) uses).
- everything else — reverse-proxied to the agent's dev server running inside the
  box.

So `https://<agent>-devbox.a2acloud.io/` reaches your running agent, while the
CLI syncs files over the same ingress. No extra ports, no bastion, no firewall
rules. Files sync with `rsync` over the tunnel; the box's own dev server handles
hot reload, so a save becomes a reload without any push step of its own.

## Requirements

- You must be **logged in** (`a2a login`) — the box runs under your account, with
  your quota and your CLI credentials injected, exactly like `a2a ssh`. If you're
  not logged in, `a2a dev` will tell you; use `--local` to run without an account.
- `rsync` on your machine (standard on macOS and Linux).

## Reference

```bash
a2a dev                      # cloud: sync + run + public URL (inside an agent repo)
a2a dev --agent <name>       # cloud dev for a named agent
a2a dev --local              # run on this machine with hot reload
a2a dev --local --host-runtime  # local, directly in this Python process
```

The first `a2a dev` after an idle period cold-starts the box (a few seconds); the
CLI waits through it. See also [SSH dev boxes](/concepts/dev-boxes) and
[Agents](/concepts/agents).
