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
uv run twine check --strict dist/*
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

Actions and uv are pinned. CI workflow token permissions are read-only; CI runs on
the same ref replace obsolete runs. The release workflow grants OIDC access only
to its PyPI upload job and repository write access only to its release-asset job.
Provider tests use native SDK mocks, not credentials or live API calls. Optional
integrations have isolated missing-extra tests.

## Test layout

The test tree mirrors the library: `tests/kernel/`, `tests/core/` (including
runtime, prompt, session and tools), `tests/plugins/`, `tests/models/` and
`tests/testing/`. Application composition, architecture and executable-example
checks stay at the test root; consumer typing contracts live in `tests/typing/`.
The architecture suite checks layer direction, feature independence (including
package and relative imports), and the provider-SDK/YAML boundaries.

## Maintain the docs

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

## Prepare a release

Set the package version in `pyproject.toml` and run `uv lock` to synchronize the
project version in `uv.lock` without upgrading dependencies. Update both the
[release notes](../releases.md) and the repository
[changelog](https://github.com/mathisarends/harnessed/blob/main/Changelog.md),
including breaking changes and upgrade instructions, then run the checks above
with all provider extras. Keep both entries marked unreleased until publication.
The build produces `dist/harnessed-<version>.tar.gz` and
`dist/harnessed-<version>-py3-none-any.whl`.
Use a clean `dist/` directory so the wheel smoke check selects exactly one version.

Validate both artifacts and the rendered package description before uploading:

```bash
uv run --locked --no-sync twine check --strict dist/*
```

### Configure Trusted Publishing once

The release workflow uses GitHub's OIDC identity with PyPI. No PyPI API token or
GitHub secret is required. In the repository settings, the `pypi` environment must
allow deployment only from tags matching `v*`.

For the first release, add a pending GitHub publisher at
[PyPI account publishing](https://pypi.org/manage/account/publishing/):

| PyPI field | Value |
| --- | --- |
| PyPI project name | `harnessed` |
| Owner | `mathisarends` |
| Repository name | `harnessed` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The workflow field is the filename, without `.github/workflows/`. If the project
already exists under your account, add the same publisher in the project's
Publishing settings instead. A pending publisher creates the project on its
first successful upload; see the
[PyPI setup guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

### Validate and publish a release

1. Merge the release preparation into `main`. Use a matching `v<version>` tag and
   ensure its commit contains `.github/workflows/release.yml`. For a draft whose
   tag does not exist yet, set its target to the final commit SHA. If a tag already
   exists, verify it points at that final commit before publishing.
2. In Actions, select **Release**, then **Run workflow** on `main` for a dry run.
   It runs the full CI matrix, builds both distributions, checks the installed
   wheel and validates metadata with Twine. Download `python-distributions` and
   `distribution-hashes` to inspect the artifacts. Manual runs never upload to
   PyPI or change a GitHub release.
3. Confirm the PyPI publisher is registered. Replace the unreleased labels in
   the release notes and changelog with the publication date, merge that change
   and target the release at that final commit. Keep both entries consistent
   with the version being published.
4. Publish the GitHub release. This triggers the release workflow, which requires
   the tagged commit to belong to `main` and its tag to match `pyproject.toml`.
   It reruns the full CI matrix and builds and verifies the distributions before
   uploading them through the `pypi` environment with Trusted Publishing and
   attestations. A branch push or a tag push alone does not publish a package.
5. Wait for the **Release** workflow to succeed. After PyPI accepts the upload,
   the workflow attaches the same wheel, source archive and `SHA256SUMS` to the
   GitHub release, replacing any provisional draft assets. Confirm the package
   is available on [PyPI](https://pypi.org/project/harnessed/).

If publication fails, inspect the failed job before retrying. An upload failure
before PyPI accepts any files can be retried after fixing the publisher setup.
PyPI files are immutable; do not rebuild or overwrite an already published
version. For a partial upload, inspect which files PyPI accepted and recover using
the original verified workflow artifacts. Duplicate uploads fail explicitly.
If only attachment of GitHub assets failed after a successful PyPI upload, rerun
failed jobs so the workflow reuses the verified artifacts without republishing.
