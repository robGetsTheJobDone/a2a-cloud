# Rust (single-skill demo)

This page is generated from the A2A Pack language SDK metadata.

## Read this first

**Go and Rust support exactly one skill.**

The Go and Rust SDKs implement the demo contract only: a single skill named `sum` that adds two numbers. The worker serves just `POST /_a2a/invoke/sum`, and the `.a2a/agent.dsl.json` they compile always declares that one skill.

A second skill is never routed and never advertised on the agent card. In Go the extra method compiles and deploys with no error anywhere, and is then simply unreachable. In Rust a second method inside the `impl A2AAgent` block does not compile at all (`E0407`); moved to a separate inherent `impl` block it compiles and is then unreachable just like Go's. Treat these two SDKs as a proof-of-concept for the sidecar worker protocol.

For an agent with more than one tool, use `a2a init --language python` or `a2a init --language typescript`.

## Scaffold

```bash
a2a init math-agent --language rust
```

## Implementation Contract

Implement the `A2AAgent` trait and call `compile_agent` / `serve_agent`.

```rust
pub struct MathAgent;

impl A2AAgent for MathAgent {
    fn definition(&self) -> AgentDefinition {
        AgentDefinition { name: "math-agent", description: "Math helper", version: "0.1.0" }
    }

    fn sum(&self, request: SumRequest) -> Result<SumResponse, String> {
        Ok(SumResponse { value: request.left + request.right })
    }
}
```

## Local Commands

```bash
cargo run -- compile
cargo run -- worker
```

## SDK Package

`a2a-pack-rs` vendored at `third_party/a2a-pack-rs`.

The compiled `.a2a/agent.dsl.json` is the sidecar contract. The sidecar
owns public A2A, MCP, frontend, and invoke endpoints; the language SDK
owns native handler execution through the worker protocol.
