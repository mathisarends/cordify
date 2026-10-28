# Repository guidance

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
exception is `models/pydantic_ai.py`, which delays optional SDK imports. Test-local
imports used for optional integration and import-failure tests are also exempt.
Do not add local imports to work around dependency cycles; move shared types or
change the dependency direction instead.
