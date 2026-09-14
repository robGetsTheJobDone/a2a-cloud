# Sandbox Runtime Security Gates

The production runtime is privileged because it needs `/dev/kvm` and
`/dev/fuse`. It must run only on nodes that are dedicated to sandbox VMs and
carry both `a2a/sandbox-isolated=true` and the matching `NoSchedule` taint.
Hosted user-agent nodes must retain only `a2a/worker=true`.
The node-join tooling must register sandbox nodes with the isolation taint before
their first kubelet heartbeat and withholds `a2a/sandbox=true` until it verifies
that no `agents` workload remains on the node.

The checked-in DaemonSet currently has a tag-only bootstrap runtime image from
the previously writable registry. It is not trusted. The dedicated-node gate
must keep the DaemonSet unscheduled until the authenticated build workflow
rebuilds it and commits `image: ...:$GITHUB_SHA@sha256:...`. The node join
script refuses sandbox mode while that digest is absent.

## Guest Network Policy

Every VM is created with a microsandbox SDK network policy before boot:

- normal sessions use `Network.public_only()` with DNS-rebinding protection;
- `network_disabled=true` sessions use `Network.none()`;
- the guest iptables/nftables setup remains a second layer for disabled
  sessions.

The namespace also has deny-by-default Kubernetes NetworkPolicies. These
controls do not replace an operational VM/TAP probe. Before launch, verify from
a real guest that public HTTPS works and that loopback, RFC1918, link-local
metadata, cluster services, and every node address fail.

## Persistent Session Isolation

The service ignores caller-provided sandbox names and returns a server-random
identifier. A lock-protected reservation is recorded before VM creation so
concurrent requests cannot replace another tenant's `replace=True` sandbox or
race past capacity. `A2A_SANDBOX_MAX_LIVE_SESSIONS` and
`A2A_SANDBOX_MAX_LIVE_PER_BUCKET` cap all live VMs, including one-shot calls,
per runtime instance and bucket. `A2A_SB_VM_MEMORY_MIB` and
`A2A_SB_VM_CPU_COUNT` are operator ceilings, not caller-overridable defaults.
Grant-created persistent sessions are bound to the creating grant ID; another
grant for the same bucket cannot execute or delete them. Failed creates,
deletes, one-shot completion, and shutdown release reservations. The Service
uses client-IP affinity because live VM handles are process-local.

Stopping a handle removes the persisted microsandbox rootfs and temporary
bridge/FUSE workspace. Failed VM creation runs the same cleanup path so image
pull or boot failures cannot accumulate mounts and disk images.

## Guest Image Policy

`A2A_SANDBOX_ALLOWED_IMAGES` is an exact, operator-owned allowlist. Production
also sets `A2A_SANDBOX_REQUIRE_IMAGE_DIGEST=true`, so every allowed reference
must contain a full `@sha256:` digest. Callers cannot select an alternate
registry, repository, tag, URL, or private host.

`A2A_SANDBOX_IMAGE_ALIASES` may map a previously published caller-facing name
such as `python:3.11-slim` to an allowlisted digest. The runtime always passes
the digest to microsandbox; it never pulls the alias tag.

The current `library/python:3.11-slim-bookworm` OCI index digest was read from
the authenticated Docker Registry v2 response on 2026-07-18. The public
`python:3.11-slim` caller alias resolves to that pinned Bookworm image so
package installation cannot silently move to a new Debian release. To rotate
it, resolve the new registry digest, review it, and update the default and
allowlist in the same change. Do not replace it with a mutable tag.

## Runtime Pin

The Python SDK is pinned to `microsandbox==0.3.14`. The production Docker build
installs the official PyPI x86_64 wheel by URL with SHA256
`2a77091a774e8fc0d27ecd0050913292cd234f3bb4bc37c7da7987557c7bd2bf`.
That wheel supplies both the native module and CLI, so no remote shell
installer runs on the image or Kubernetes host.

The privileged image base is also pinned to the Docker Registry OCI index
digest `sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf`
for `python:3.12-slim`, verified on 2026-07-11.
