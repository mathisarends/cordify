# Harnessed architecture

This file records the implemented package boundaries. Public behavior belongs in
[the SDK reference](docs/reference/sdk.md) and the topic guides; future proposals
belong in [the roadmap](docs/contributing/roadmap.md).

## Layers

| Layer | Responsibility | Allowed dependencies |
| --- | --- | --- |
| `kernel` | Reversible plugin lifetimes, registration ownership, services, effects, hooks and per-mount state | Standard library only |
| `core` | Default agent mechanisms and provider-independent domain contracts | Kernel and Pydantic |
| `plugins`, `models`, `integrations` | Opt-in features, provider integrations and telemetry | Core, kernel and each feature's own optional dependencies |
| `agent.py` and public facades | Application SDK and explicit public exports | The lower layers |
| `testing` | Scripted models, fakes, assertions and pytest support | Library layers; production code never imports testing |

Protocols belong to their consumer. The loop's `Model`, `SessionStore`, `Approver`,
`Summarizer` and `TokenCounter` contracts live in core. Feature-specific contracts
such as `FileSystemBackend` and `ShellBackend` live with their implementations and
plugins. Plugins do not import other plugins. Shared behavior crosses a core
protocol instead.

## Package layout

- `kernel/` is independent of the agent and third-party packages.
- `core/runtime/` owns live sessions, turn and step orchestration, model calls,
  tool execution, output validation, streaming, inbox handling and controls.
- `core/session/` owns immutable durable events, logs, projections, codecs,
  persistence contracts and session-owned files.
- `core/tools/` owns declarations, argument validation, references, registration,
  policies, resource scheduling and output limits.
- `core/prompt/` owns instruction composition and dynamic context.
- `plugins/` groups each opt-in feature with its backend contract and local
  implementation. A feature can become a package when its size requires it, as
  with the shell's platform-specific implementation.
- `models/pydantic_ai.py` is the only provider-SDK integration. It imports its
  optional dependency on use. Only `plugins/skills.py` imports YAML.
- `integrations/opentelemetry.py` implements the optional `tracer` slot against
  an application-owned OpenTelemetry provider. Import it explicitly with the
  `otel` extra installed; SDK and feature facades never import OpenTelemetry.
- `plugins/mcp.py` owns optional MCP clients, transport lifetimes and schema
  validation. Import it explicitly with the `mcp` extra installed. Core's
  `ToolRegistry.register_external()` owns schema-defined remote registrations
  on the existing scheduling, policy and execution pipeline without importing MCP.
- `events/`, `hooks.py` and `errors.py` expose public names for core domain values,
  hooks and the error tree.
- `agent.py` resolves the model/instructions/plugin recipe and opens owned sessions.

Do not restore `seams`, `adapters`, `app`, `llms`, `presets` or native provider
clients. Applications use the small root SDK, `harnessed.plugins`,
`harnessed.models`, `harnessed.errors`, `harnessed.hooks` and `harnessed.events`.
Built-in event classes have full names ending in `Event`, re-exported directly
from `harnessed.events` with the same names as their core definitions.
Internal modules import their owning layer rather than the root facade.

## Runtime and ownership invariants

- `Agent` resolves `model`, `system_prompt` and `plugins`; `plugin_validation`
  controls session startup readiness. Defaults are normal plugins replaced through
  slots. Startup validation runs after session services settle and before the
  runtime starts. The kernel exposes dependency diagnostics while retaining
  deferred-service lifetimes.
- `RuntimeServices` has no slot and is installed first. It provides the tool
  registry, system prompt, calibrated token counter and context budget, so replacing
  the `loop` slot keeps them.
- `TurnRunner` and `StepRunner` own orchestration. Typed runtime components receive
  concrete dependencies; free functions do not receive `Session` as a pseudo-self.
- Python tools are `@tool` methods; remote schema-defined tools use the explicit
  `register_external()` integration boundary. A scheduled call retains its `PreparedToolCall` binding
  instead of silently following a replacement registration.
- Plugins inherit the small `Plugin` base. Configuration models live at module
  scope. Mutable mount state is initialized by `PluginState` or `setup` and remains
  available until all cleanup effects finish.
- Plugins declare service requirements explicitly with `needs`; `Inject` binds
  attribute and parameter values without inferring lifetime dependencies.
- Registration ownership uses public `Context.register()` and
  `RegistrationState.active`. Higher layers do not inspect private kernel state.
- Prompt metadata is normalized into typed `PromptContribution` values at the agent
  boundary and rendered by `PromptComposition`. SystemSection declarations bind
  to recipe identities. SystemPrompt orders strings, sections, plugin groups and
  one globally resolved rest slot; remaining sections append by configured plugin
  and declaration order unless Unplaced.ERROR requires full placement. Its outer
  format and unplaced policy govern nested groups. SystemPromptStore holds the
  immutable session snapshot. Runtime section delivery is explicit: ERROR rejects,
  CONTEXT owns guidance for the mount activation, and IGNORE never runs providers.
  Active registrations hide runtime guidance during suspension and disposal;
  historical request records remain inspectable without restoring mount context.
- Durable events use explicit versions and dotted discriminators. Replay restores
  state without rerunning tools. Output restoration matches registered Python
  validator identity and schema; equal schemas alone are insufficient.
- Feature and model facades use explicit re-exports. Only the SDK root is lazy,
  preserving standalone kernel imports; its type stub matches the runtime exports.

## Verification

`tests/test_architecture.py` enforces dependency direction, feature independence,
provider/YAML isolation, public exports and kernel isolation. Tests mirror their
owning packages under `tests/kernel/`, `tests/core/`, `tests/plugins/`,
`tests/models/` and `tests/testing/`; consumer typing contracts live in
`tests/typing/`. See [development and CI](docs/contributing/development.md) for the
commands and platform matrix.
