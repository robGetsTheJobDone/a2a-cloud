# A2A sidecar base images

These Dockerfiles build the language worker images selected by the control
plane's Agent DSL build matrix:

- `a2a-sidecar-node` for TypeScript and JavaScript
- `a2a-sidecar-go` for Go
- `a2a-sidecar-rust` for Rust
- `a2a-sidecar-jvm` for Java
- `a2a-sidecar-dotnet` for .NET

Each image includes the Python `a2a-pack` CLI for the common sidecar runtime
and installs `/usr/local/bin/a2a-sidecar-build`. User source images call:

```sh
a2a-sidecar-build <language>
```

The build command validates `.a2a/agent.dsl.json` and then runs conservative
language defaults. A project can override those defaults with a `Makefile`
`build` target. The runtime command remains explicit in
`agent_dsl.entrypoint.command`; the control plane starts that worker command
beside `a2a sidecar`.
