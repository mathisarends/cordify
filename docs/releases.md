# Releases and compatibility

Harnessed 0.1.x is alpha software. Its public API and persistence contracts are
still evolving before 1.0.

## Compatibility policy

- Minor releases during 0.x may introduce breaking API or storage changes. Release
  notes identify these changes and any required upgrade steps.
- Patch releases within a minor line are intended for compatible fixes. Pin the
  exact package version for reproducible deployments and persisted workflows.
- Session records have an explicit format version. The initial release uses
  version 1; incompatible core record changes require a new version. Unsupported
  versions fail explicitly, and no automatic format migrations are provided.
- Back up persisted sessions before upgrading. Application-defined events and
  versioned output contracts have their own validation requirements; see
  [custom events](guides/events.md) and
  [structured answers](guides/structured-output.md).

Python 3.12, 3.13 and 3.14 are supported on Linux, Windows and macOS. Provider
integrations are optional extras; the standalone kernel uses only the standard
library. Harnessed is distributed under the
[MIT License](https://github.com/mathisarends/harnessed/blob/main/LICENSE).

## 0.1.0 (unreleased)

Initial public release, with the following capabilities:

- An asynchronous agent SDK with tools declared as plugin methods, reversible
  plugin lifetimes, dependency injection, hooks and replaceable defaults.
- Optional PydanticAI integrations for OpenAI-compatible providers, Anthropic and
  Google, including structured answers and typed streaming events for text,
  reasoning, tool argument previews and approvals.
- Append-only session logs, JSONL persistence, crash recovery, forks, queued input
  and pause/resume/stop controls. In-memory session retention can be bounded or
  disabled for one-shot work.
- Tool approvals, filesystem and shell tools, skills, context compaction, output
  limits and session-owned spill artifacts.
- An offline testing toolkit, executable examples, typed public APIs and
  [session memory and replay benchmarks](benchmarks/sessions.md).

Provider-specific built-in tools, audio/video output and hosted resumable
operations are not exposed by the current model contract. Subagents, MCP and OTel
integrations are deferred. Provider tests use native SDK mocks and mock HTTP
responses; they do not validate live API availability.
