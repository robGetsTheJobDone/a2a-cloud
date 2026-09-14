# LLM credentials & model compatibility

A2A Cloud routes user-provided model keys through LiteLLM. That gives chat and
LLM-capable agents one OpenAI-compatible interface while preserving the user's
chosen provider, model, usage, and pricing metadata.

## How model setup works

In the dashboard, open **LLM credentials** and choose a provider. The model
dropdown is populated from LiteLLM's public model cost map, so it includes the
chat/completion models LiteLLM knows how to route and price.

Known providers, such as OpenAI, Anthropic, Gemini, Moonshot/Kimi, OpenRouter,
Groq, Mistral, Cohere, xAI, DeepSeek, and Ollama, get a preset API base URL and
default model. Additional providers discovered from LiteLLM still appear in the
catalog, but you must enter the provider's OpenAI-compatible base URL before
saving the key.

## What agents receive

When an agent asks for `ctx.llm`, it receives credentials shaped like:

```env
base_url=https://llm.a2acloud.io/v1
api_key=<scoped LiteLLM key or grant>
model=<platform model alias>
```

Agent code should call that endpoint as an OpenAI-compatible Chat Completions
API. The control plane registers the user's real provider key behind the alias,
so the agent does not need provider-specific routing logic.

## Pricing and tracking

LiteLLM is the source of truth for usage and price attribution. The control
plane registers provider-aware models, for example `moonshot/kimi-k2.6` for
Moonshot/Kimi or `xai/grok-4` for xAI, so LiteLLM can match the model against
its cost map.

If a model is in LiteLLM's cost map, token usage and spend are recorded with the
provider's pricing. If a custom model is not in the cost map, the request can
still run through the compatibility layer, but LiteLLM may not be able to attach
provider pricing unless the gateway has pricing metadata for that model.

## Examples

Moonshot/Kimi:

```text
Provider: Kimi / Moonshot
API base URL: https://api.moonshot.ai/v1
Model: kimi-k2.6
Extra request body: {"thinking":{"type":"disabled"}}
```

OpenRouter:

```text
Provider: OpenRouter
API base URL: https://openrouter.ai/api/v1
Model: openrouter/auto
```

Custom LiteLLM-compatible provider:

```text
Provider: Custom / LiteLLM
API base URL: https://your-provider.example/v1
Model: provider/model-name
```

Use the dashboard dropdown when possible. It keeps the saved model aligned with
LiteLLM's compatibility and pricing catalog.
