# SSH dev boxes

`a2a ssh <agent>` drops you into a throwaway dev box for an agent — a shell with
**Node, Python, a2a-pack, and the agent's repo already there**. The box scales to
zero when you disconnect, so it costs nothing idle and wakes on the next connect.

No VM to provision, no dependencies to install, no repo to clone. One command:

```bash
a2a ssh my-agent
```

You land in `~/my-agent`, on the same runtime your agent deploys on.

Inside an agent repo you can skip the name entirely — `a2a ssh` reads it from
`a2a.yaml` (searching upward from the current directory):

```bash
cd my-agent/
a2a ssh
```

## What's in the box

Every dev box comes up with:

- **Node 20** and **Python 3.11** on the `PATH`
- **a2a-pack** pre-installed **and already logged in as you** — your CLI
  credentials are injected per session, so `a2a deploy`, `a2a agents`, and
  friends work immediately
- **git** and the agent's source repo, already cloned into `~/<agent>`
- your own home directory with a real shell (`bash`), `sudo`, and TTY

It runs the same base image family as your deployed agents, so what works in the
box works in production.

## Connect from anywhere

`a2a ssh` writes an entry to your `~/.ssh/config`, so the box is reachable from
any OpenSSH-based tool by its host alias `<agent>.a2a`:

```bash
ssh my-agent.a2a                      # a plain shell
scp ./data.csv my-agent.a2a:~/        # copy files in
rsync -av ./out/ my-agent.a2a:~/out/  # sync a directory
```

### VS Code / Cursor / JetBrains

Because it rides a standard SSH `ProxyCommand`, remote IDEs work out of the box:

- **VS Code / Cursor** — *Remote-SSH: Connect to Host…* → pick `my-agent.a2a`
- **JetBrains Gateway** — connect over SSH to the same host alias

You get a full remote editing session against the agent's repo, with the agent's
own toolchain underneath.

### Tools that want a host and port

Some SSH clients (GUI sftp tools, mobile clients, database tools with SSH
tunneling) can't read `~/.ssh/config`. For those, serve the box on a local TCP
port:

```bash
a2a ssh my-agent --tunnel --port 2222   # leave running; omit --port to auto-pick
```

Then point the tool at:

| Field    | Value                                        |
| -------- | -------------------------------------------- |
| Host     | `127.0.0.1`                                  |
| Port     | `2222`                                       |
| User     | `dev`                                        |
| Auth     | key file — there is **no password**          |
| Key file | `~/.a2a/ssh/my-agent`                        |

### Scripting

`a2a ssh my-agent --json` prints the full connection details — user, identity
file, known-hosts file, proxy command — as JSON with absolute paths, ready to
feed into other tools:

```bash
a2a ssh my-agent --json | jq -r .identity_file
```

## How it connects

The cluster ingress is HTTP-only — there's no exposed SSH port. Instead the box
runs `sshd` on localhost and a small bridge tunnels SSH over a WebSocket through
the same HTTPS ingress your agents use. The `a2a` CLI ships the
`ProxyCommand` that speaks that tunnel, so you never manage a bastion, a
port, or a firewall rule. This is the same pattern tools like Cloudflare Access
use for SSH-over-HTTPS.

## Security

A dev box is scoped and disposable by design:

- **Ephemeral key** — `a2a ssh` generates a throwaway SSH keypair per agent under
  `~/.a2a/ssh`; only that key can log in.
- **Signed transport** — each connection carries a short-lived Ed25519 grant
  scoped to that one box, verified at the tunnel before SSH even begins. Nothing
  shared, nothing stored, nothing to leak.
- **Scoped repo access** — the box clones the repo with a fresh, read-scoped
  Gitea token, never a platform admin credential.
- **Scale-to-zero** — when you disconnect, the box drains and scales to zero, so
  there's no idle surface sitting around.

Two independent checks gate every session: the transport grant, and your SSH
key. Neither one alone gets you in.

## Reference

```bash
a2a ssh                            # inside an agent repo: agent name from a2a.yaml
a2a ssh <agent>                    # open a shell (waking the box if idle)
a2a ssh <agent> --print            # print the host alias + connection tips, don't connect
a2a ssh <agent> --json             # connection details as JSON (absolute paths)
a2a ssh <agent> --tunnel --port N  # local host:port for tools without ssh-config support
```

The first connect after an idle period cold-starts the box (a few seconds); the
CLI waits through it automatically. See also [Agents](/concepts/agents) and
[Grants](/concepts/grants).
