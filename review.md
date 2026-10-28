# Remaining review work

The approved [API target](api-target.md) and [structure target](structure-target.md)
are authoritative. This file contains open work only. Completed findings and
superseded provider designs have been removed; their details remain in Git history.
Do not add more native provider clients, retry policies, or presets. PydanticAI is
an optional Direct API integration; the Harnessed kernel, loop and log remain independent.

## P0 — Runtime and public contracts

### P0-6 — Complete neutral model/message contracts

- Finish the Model / ModelInfo / ModelResponse / ModelStream migration and remove
  all stream-only LLM compatibility paths. Wire the model's context window into
  budgets; do not silently assume 128k for unknown models.
- Rename the remaining message reasoning field and type stop reasons throughout;
  verify accept/continue/fail max-token behavior through the public result API.
- Support multimodal tool results and model history, including the output-limit,
  spill and token-counting paths. Preserve native provider response state.
- Finish moving the remaining feature contracts to their consumers; FS is now
  collocated with its implementation and bounded, schema-validated tools.
- Allow asynchronous provider token counting, retaining a cheap approximation.

### P0-7 — Finish the public facade and remove duplicate paths

- Remove llms/, presets.py, Harness and the old presets after their remaining
  consumers migrate. The application facade is now independent of Harness.
- Finish converting optional built-ins to CapWords classes using public APIs.
- Reduce root exports, sort/check __all__, and remove duplicate class names,
  AgentHandle and compatibility aliases once consumers migrate.

## P1 — Runtime design and ergonomics

### P1-1 — Split the driver and make state explicit

- Split loop.py into runtime stepper, executor, inbox/control and result modules.
  Use dedicated services instead of making every plugin depend on the driver.
- Replace interacting boolean flags with SessionStatus and centralized legal
  transitions. Implement the approved pause/resume/stop/abort boundaries and
  durable outcomes; update race/restart tests and examples.

### P1-2 — Structured results and a small UI stream

- Keep turn-scoped run/stream completion, usage, stop reason, IDs and tool pairs.
- Add generic typed output with the approved PydanticAI-backed response format /
  final-answer tool fallback and a bounded validation retry.
- Add the stable typed StreamEvent union and terminal TurnFinished result;
  expose raw durable/transient subscriptions separately.
- Expose the public Session API without the legacy agent.session indirection.

### P1-4 — Finish the two plugin authoring styles

- Keep ParamSpec factory signatures, immutable recipe keywords, and validated
  class Config. Mutable mount state belongs in setup; clients/locks stay shared.
- Finish converting optional built-ins to CapWords classes.

### P1-6 — Loop configuration and composition

- Remove the legacy services.py and preset setup after migrating their remaining
  consumers. AgentLoop now provides its registry/prompt/runtime services, and the
  application path no longer installs implicit compaction or spill stores.
- Keep the tested single-install/single-disposer behavior and public settle barrier.

### P1-7 — Events, names and codec

- Use consistent class-derived type tags with legacy decoding aliases.
- Register event kinds once, add version/migration support and a Pydantic union
  codec. Keep frozen immutable values and reject corrupt/unknown records.
- Type step/turn/control outcomes rather than accepting arbitrary strings.

### P1-8 — Boundary validation

- Use Pydantic for external options/configuration and the persistence codec;
  keep internal hot-path values as frozen dataclasses.
- Replace remaining duplicated hand-written option validators with declarative
  constraints. Put FS/range limits in the tool's JSON schema.
- Retain strict tool-form validation, registration-time schemas and cached plans.

### P1-9 — Tool declarations and context

- Document cooperative cancellation of sync tools; keep resources and stable
  result ordering. Preserve explicit ownership and override=True conflicts.

### P1-10 — Finish the PydanticAI migration

- The Direct API bridge and native provider mock spikes are implemented.
- Remove DeepSeekLLM, its wire models/SSE parser, the retry base/RetryPolicy and
  deepseek extra. Keep optional openai/anthropic/google extras on pydantic-ai-slim.
- Use upstream context-window metadata when available and explicit overrides;
  document unsupported native request/response capabilities clearly.
- Verify missing-extra errors, owned/borrowed client lifetimes, stream abort,
  failure mapping and Python 3.12–3.14 against the completed migration.

### P1-11 — Finish Option C across the default application

- Instructions then plugin guidance, literally and in order, fixed per session;
  placement/replacement/omission, file sections and origins already work.
- Remove legacy persona/agents_md and order/stable prompt APIs from defaults.

## P2 — Structure, typing, docs and testing

### P2-1 — Type hook signatures

- Publish Next[R] and type emit, bail and waterfall callbacks so invalid event
  or return types fail in ty. Type all examples and verify positive/negative
  consumer cases. Existing tool/plugin/Inject/run contracts already run in CI.

### P2-2 — Remove local-import cycle workarounds

- Finish runtime/tool/kernel dependency cleanup, then enforce PLC0415. Delayed
  optional SDK imports in models/pydantic_ai.py need an explicit exception.

### P2-4 — Frozen value hashing

- Define/document deliberate hashability for values containing immutable
  mappings; frozen does not imply that all payloads can be hashed.

### P2-5 — Typed message roles

- Replace freely combinable role/field values with the approved tagged message
  union, preserving migration/replay and multimodal/provider metadata.

### P2-6 — Testing package

- ScriptedModel exists. Add InMemoryFS, FakeShell, InMemoryPersistence, reply/call,
  tool-call assertions and the pytest fixture/plugin. Move the extractive fake
  compactor here; production compaction uses the session model.

### P2-7 — Complete structure-target.md

- Collocate each feature's contract, implementation and plugin. Remove seams/,
  adapters/, _context/, observe/, agents/ and transition re-exports.
- Finish splitting the runtime and mirror the new package structure in tests.
- Enforce the target layer, feature-independence and SDK/YAML isolation rules.

### P2-8 — English documentation and examples

- Replace the oversized README with the current application quickstart and links.
- Add guides/concepts/reference docs, example snippets and a strict docs CI build.
- Keep early examples focused on the application API and explicit plugin sugar;
  migrate all later examples off removed APIs, with real control/context examples.

## Target features and policies

- Unify compaction into Compaction(trigger, summarizer, focus, retention). Provide
  at_ratio/every_turns triggers, model summarization, pre-request side-effect
  boundaries and one reactive context-error retry.
- Spill is session-owned, bounded by retention, with ranged reads and cleanup.
- CacheBreakpoints must use actual native provider cache controls or be removed;
  do not export an inert feature. Usage events already carry cache metrics.
- Add the approved Subagent child-session/log mechanism and inherited approvals.
- Add Skill/Skills with lazy discovery/load/read, safe path handling and optional
  YAML as specified in api-target.md. MCP, OTel, UI, RAG and eval systems remain
  outside the current implementation scope.
