# Repository guidance

## Imports

Use absolute `cordify` imports when crossing package boundaries. Parent-relative
imports (`from ..` and `from ...`, including deeper forms) are not allowed.
Single-dot relative imports within the same package remain allowed.

```python
from cordify.adapters.shell import LocalShell
from cordify.kernel import Context, Inject, plugin
from cordify.seams.shell import ProcResult, Shell
```

Plugin implementations and examples must use the public `cordify.kernel` facade
for kernel APIs, such as `Context`, `Inject`, `plugin`, and `hook`. Do not import
these through `cordify.kernel.context`, `.di`, or `.plugin`. Keep the facade's
exports in `cordify/kernel/__init__.py` stable when reorganizing kernel internals.
Kernel implementation modules may use sibling imports to avoid import cycles.

Keep imports pointed at their owning layer. Layers from lowest to highest are
`kernel`, `seams`, `core`, `plugins` / `adapters`, and `presets`. A layer may
depend on lower layers; the standalone kernel must stay independent of agents
and third-party packages. Internal modules should import their dependencies from
the owning module or layer rather than the top-level `cordify` facade.

Ruff enforces the parent-relative import rule with `TID252` and
`ban-relative-imports = "parents"`. Run `uv run ruff check .` before committing;
the same rule also runs through pre-commit and CI.
