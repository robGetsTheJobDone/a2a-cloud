# Go (single-skill demo)

This page is generated from the A2A Pack language SDK metadata.

## Read this first

**Go and Rust support exactly one skill.**

The Go and Rust SDKs implement the demo contract only: a single skill named `sum` that adds two numbers. The worker serves just `POST /_a2a/invoke/sum`, and the `.a2a/agent.dsl.json` they compile always declares that one skill.

A second skill is never routed and never advertised on the agent card. In Go the extra method compiles and deploys with no error anywhere, and is then simply unreachable. In Rust a second method inside the `impl A2AAgent` block does not compile at all (`E0407`); moved to a separate inherent `impl` block it compiles and is then unreachable just like Go's. Treat these two SDKs as a proof-of-concept for the sidecar worker protocol.

For an agent with more than one tool, use `a2a init --language python` or `a2a init --language typescript`.

## Scaffold

```bash
a2a init math-agent --language go
```

## Implementation Contract

Implement the `a2apack.Agent` interface and call `a2apack.CompileAgent` / `a2apack.ServeAgent`.

```go
type MathAgent struct{}

func (MathAgent) Definition() a2apack.AgentDefinition {
    return a2apack.AgentDefinition{Name: "math-agent", Description: "Math helper", Version: "0.1.0"}
}

func (MathAgent) Sum(ctx context.Context, request a2apack.SumRequest) (a2apack.SumResponse, error) {
    return a2apack.SumResponse{Value: request.Left + request.Right}, nil
}
```

## Local Commands

```bash
go run . compile
go run . worker
```

## SDK Package

`a2acloud.io/a2a-pack-go` vendored at `third_party/a2a-pack-go`.

The compiled `.a2a/agent.dsl.json` is the sidecar contract. The sidecar
owns public A2A, MCP, frontend, and invoke endpoints; the language SDK
owns native handler execution through the worker protocol.
