# Repository guidance

## Annotations and documentation

Use `from __future__ import annotations` only when a module needs deferred names,
such as forward references or types imported under `TYPE_CHECKING`. Python 3.12+
already supports the native generic syntax; that alone needs no future import.
Prefer `Self` for constructors returning the current class. Avoid boilerplate
module docstrings that restate the filename. Document public behavior and useful
invariants where readers need them.

## Imports

Use absolute `harnessed` imports when crossing package boundaries. Parent-relative
imports (`from ..` and `from ...`, including deeper forms) are not allowed.
Single-dot relative imports within the same package remain allowed.

```python
from harnessed.plugins.shell import LocalShell, ProcResult, ShellBackend
from harnessed.kernel import Context, Inject, plugin
```

Plugin implementations and examples must use the public `harnessed.kernel` facade
for kernel APIs, such as `Context`, `Inject`, `plugin`, and `hook`. Do not import
these through `harnessed.kernel.context`, `.di`, or `.plugin`. Keep the facade's
exports in `harnessed/kernel/__init__.py` stable when reorganizing kernel internals.
Kernel implementation modules may use sibling imports to avoid import cycles.

Keep imports pointed at their owning layer. The target layers, from lowest to
highest, are `kernel`, `core`, `plugins` / `models` / `integrations`, then
`agent.py` and public facades. The standalone kernel uses only the standard
library. Core may depend on kernel and Pydantic. Optional features depend on
core, kernel and their own extras; features never import another plugin.
Only `models/pydantic_ai.py` imports PydanticAI; only `plugins/skills.py` imports
YAML. Internal modules import their owning layer, not the root facade.

Follow `api-target.md` and `structure-target.md`. Do not restore `seams`,
`adapters`, `app`, `llms` or `presets`, or add native provider clients. Prefer
canonical `core.messages` / `core.model` imports inside the library. Applications
use the small root SDK, `harnessed.plugins`, `harnessed.models` and `harnessed.hooks`.

Ruff enforces the parent-relative import rule with `TID252` and
`ban-relative-imports = "parents"`. Run `uv run ruff check .` before committing;
the same rule also runs through pre-commit and CI.

Keep production imports at module scope. Ruff enforces `PLC0415`; the explicit
exceptions are `models/pydantic_ai.py` (optional SDK) and `plugins/skills.py`
(optional YAML), which delay their extra's imports. Test-local
imports used for optional integration and import-failure tests are also exempt.
Do not add local imports to work around dependency cycles; move shared types or
change the dependency direction instead.

## Python style and boundaries

Keep public and cross-module signatures fully annotated, including return values
and collection element types. Contain `Any` and reflection at genuinely dynamic
boundaries such as schema construction and heterogeneous registries. Do not use
`Any`, `getattr`, or casts to hide a missing domain contract.

Prefer `StrEnum` for closed domain choices, states, outcomes and policies. Event
and message discriminators retain a concrete `Literal[Enum.MEMBER]` so unions
remain discriminated. Custom plugins may define their own discriminator enums.
Use stable dot-separated event tags, such as `assistant.message`, `tool.progress`
and `step.end`. JSON-schema keys remain ordinary strings.

Plugins inherit the small `Plugin` base and declare tools only as `@tool` methods.
Use module-level configuration models. Class-level `needs=` and `slot=` metadata
are supported; do not introduce a second decorator-based plugin class system.
The filesystem backend protocol is `FileSystemBackend`, its local implementation
is `LocalFileSystem`, and the application plugin remains `Filesystem`.

Use `logger = logging.getLogger(__name__)` in modules that log. Write independent
attribute assignments on separate lines. Put `__all__` at the bottom of modules
that define it. Single-dot sibling imports remain preferred within a package;
cross-package imports stay absolute.

Keep lifecycle ownership behind public kernel operations. Higher layers must not
call private Context lifecycle checks or inspect private registration state.
Stateful turn and step orchestration belongs to typed runtime components, not
free functions receiving a Session as a pseudo-`self` argument.

There is no released storage/API compatibility contract yet. Keep one canonical
current format; remove legacy migrations and transitional aliases rather than
adding compatibility layers for unreleased formats. Retain an explicit format
version for future released changes.

Normalize plugin prompt metadata into typed `PromptContribution` values at the
agent boundary. `PromptComposition` renders these contributions. Tool scheduling
captures one `PreparedToolCall`; it must not silently follow replaced bindings.

Use `Context.register()` for owned registrations and `RegistrationState.active`
for visibility; do not inspect private Context state from higher layers. Public
event classes live behind the `harnessed.events` namespaces. Feature/model facades
use explicit re-exports; only the SDK root stays lazy to preserve kernel isolation,
with its type stub checked against the runtime export table.

Output restoration matches a registered validator identity and its schema, never
JSON Schema alone. Use versioned `OutputContract` keys for persisted application
workflows and bump the key when Python validation behavior changes.
