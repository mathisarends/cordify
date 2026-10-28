# Contributing

Use Python 3.12–3.14 and the root-level `harnessed` package. Read `AGENTS.md`
for layer and import rules. Install the locked environment with
`uv sync --locked --all-extras` and hooks with `uv run pre-commit install`.

## Validation boundaries

Validate external values (user configuration, files, network payloads, and model
arguments) with Pydantic. Use frozen dataclasses for internal events and values.
Do not add repeated validation/reflection to token or tool dispatch paths.

Class plugins may define a module-level Pydantic configuration model and assign
it with `Config = MyPluginConfig`; the constructor validates its keyword
configuration. Annotate `config: MyPluginConfig` for static checking. Allocate
mutable per-mount state in `setup()`, not
in the recipe constructor. Register resource cleanup with `ctx.effect()` as
soon as allocation succeeds; these effects also run when setup fails. The
recipe's shared clients and locks are not deep-copied. Applications retain
ownership of shared client objects unless the plugin explicitly owns them.

Functional plugins should declare temporal composition with `needs=[...]` and
resolve those services through their context. Use `Inject` for per-call services
in tools/hooks. `Inject[T | None]` is optional and does not drive restarts.
For a named service use `Annotated[T, InjectMarker(key)]`.

Hook priorities run in ascending order: `Priority.GUARD`, `Priority.DEFAULT`,
then `Priority.OBSERVE`. Equal priorities keep registration order. Use explicit
`owner=ctx` when registration happens outside the plugin's activated call chain.
Duplicate tool or prompt-section names require `override=True`.

## Verification

Run `uv run pre-commit run --all-files` and `uv run pytest` before submitting.
The static consumer contracts in `tests/typing` run in ty alongside the library,
examples and scripts. CI checks all supported Python versions on Linux, Windows
and macOS, then builds and smoke-tests the installed wheel outside the checkout.
