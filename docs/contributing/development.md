# Development and CI

The package lives directly in `harnessed/`. Use one uv-managed `.venv`; alternate
Python versions use managed interpreters and isolated runs instead of permanent
`.venv-312` or `.venv-313` directories.

```bash
uv sync --locked --all-extras
uv run ruff check .
uv run ruff format --check .
uv run ty check --error-on-warning
uv run pytest
uv run pre-commit run --all-files
uv run mkdocs build --strict
uv build --no-sources
uv run python scripts/check_wheel.py
```

`uv.lock` pins development and optional provider dependencies. Install the commit
hooks with `uv run pre-commit install`. Ruff enforces modern syntax and absolute
imports across package boundaries. Ty checks production code, examples, scripts
and positive/negative consumer contracts. Expected failures must remain real
errors; unused ignore comments are errors too.

Tests mirror the owning package where features have migrated. Pytest uses its
importlib mode so modules such as `core/test_compaction.py` and
`plugins/test_compaction.py` can share a basename without import collisions.

## CI coverage

| Check | Coverage |
| --- | --- |
| Quality | Ruff, ty and repository pre-commit hooks |
| Documentation | Strict MkDocs build, navigation, local links and anchors |
| Runtime | Linux, Windows and macOS x Python 3.12, 3.13 and 3.14 |
| Platform types | ty with the matching Python version and OS in each cell |
| Examples | Offline scripts executed by the test suite |
| Distribution | Build and install the wheel outside the checkout |

Actions and uv are pinned. Workflow token permissions are read-only; runs on the
same ref replace obsolete runs. Provider tests use native SDK mocks, not credentials
or live API calls. Optional integrations have isolated missing-extra tests.

## Maintain the docs

The test tree mirrors the library: `tests/kernel/`, `tests/core/` (including
runtime, prompt, session and tools), `tests/plugins/`, `tests/models/` and
`tests/testing/`. Application composition, architecture and executable-example
checks stay at the test root; consumer typing contracts live in `tests/typing/`.
The architecture suite checks layer direction, feature independence (including
package and relative imports), and the provider-SDK/YAML boundaries.

The README contains the core SDK and links. Put explanations under `docs/guides/`,
value/runtime contracts under `docs/concepts/`, API summaries under `docs/reference/`
and contributor material here. Add a navigation entry in `mkdocs.yml` for each new
page. Keep all public documentation and examples in English.

Preview with `uv run mkdocs serve`. The [MkDocs CLI](https://www.mkdocs.org/user-guide/cli/)
describes strict builds; the project also elevates local link and anchor validation
to warnings so CI catches broken references. Building does not publish the site.

MkDocs belongs to the development dependency group. The installed package does not
depend on it or import it. The wheel smoke check verifies that documentation files
and development tools stay outside the distribution and its runtime dependencies.
