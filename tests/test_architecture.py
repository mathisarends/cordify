import ast
import importlib
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "harnessed"
LAYERS = {"kernel": 0, "core": 1, "plugins": 2, "models": 2, "integrations": 2}
# Transitional packages are removed as their features move to the target structure.
LEGACY_LAYERS = {"seams": 1, "adapters": 2}


def test_imports_follow_layer_direction_and_public_boundaries():
    violations = []
    ranks = LAYERS | LEGACY_LAYERS
    for layer, rank in ranks.items():
        for path in (PACKAGE / layer).rglob("*.py"):
            parts = path.relative_to(ROOT).with_suffix("").parts
            package = ".".join(parts[:-1])
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.ImportFrom):
                    modules = [
                        importlib.util.resolve_name(
                            "." * node.level + (node.module or ""),
                            package,
                        )
                        if node.level
                        else node.module
                    ]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                for module in modules:
                    if not module or not module.startswith("harnessed"):
                        continue
                    if layer == "plugins" and module.startswith("harnessed.kernel."):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: use harnessed.kernel facade"
                        )
                    target = module.split(".")[1:2]
                    if target == ["_exports"]:
                        continue
                    if not target or target[0] not in ranks:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: facade {module}"
                        )
                    elif ranks[target[0]] > rank:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: upward {module}"
                        )
    assert violations == []


def test_optional_integrations_and_features_are_isolated():
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""] if node.level == 0 else []
            for module in imported:
                if module == "pydantic_ai" or module.startswith("pydantic_ai."):
                    assert relative == "models/pydantic_ai.py", relative
                if module == "yaml":
                    assert relative == "plugins/skills.py", relative
                if relative.startswith("plugins/") and path.name != "__init__.py":
                    assert not module.startswith("harnessed.plugins."), relative


def test_kernel_import_is_independent_of_agents_adapters_and_third_party_packages():
    code = """
import sys
from harnessed.kernel import Context, PluginBase, Bundle, Event, Key, plugin
for layer in ('core', 'seams', 'plugins', 'adapters', 'presets'):
    assert not any(name == 'harnessed.' + layer or name.startswith('harnessed.' + layer + '.')
                   for name in sys.modules), (layer, sorted(sys.modules))
assert 'httpx' not in sys.modules
assert 'pydantic' not in sys.modules
assert not any(name.startswith('opentelemetry') for name in sys.modules)
print('standalone kernel')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_validation_is_explicit_and_otel_is_not_added():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == ["pydantic>=2.12,<3"]
    assert "otel" not in project["optional-dependencies"]
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("opentelemetry") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("opentelemetry")


@pytest.mark.parametrize(
    "module", ["harnessed", "harnessed.core", "harnessed.plugins", "harnessed.seams"]
)
def test_public_exports_resolve_to_their_canonical_classes_and_functions(module):
    facade = importlib.import_module(module)
    for name, (canonical, attribute) in facade._EXPORTS.items():
        assert getattr(facade, name) is getattr(importlib.import_module(canonical), attribute)


def test_package_root_has_only_public_entrypoints_and_no_transition_modules():
    assert {path.name for path in PACKAGE.glob("*.py")} == {
        "__init__.py",
        "_exports.py",
        "presets.py",
    }
    assert not (PACKAGE / "std").exists()


async def test_typed_keys_have_identity_and_work_with_temporal_needs():
    from harnessed.kernel import Context, Key, plugin

    first, second = Key[str]("backend"), Key[str]("backend")
    seen = []

    @plugin(needs=[first])
    async def feature(ctx):
        seen.append(ctx[first])
        yield

    async with Context() as ctx:
        ctx.provide(second, "other")
        handle = await ctx.plugin(feature())
        assert handle.status == "waiting"
        remove = ctx.provide(first, "one")
        await ctx.settle()
        assert seen == ["one"] and ctx[second] == "other"
        await remove()
        await ctx.settle()
        assert handle.status == "waiting"


async def test_kernel_registration_recipes_extend_lifetimes_without_agent_imports():
    from harnessed.kernel import Context
    from harnessed.kernel import PluginBase as Plugin

    class Sink:
        def __init__(self):
            self.callbacks = []

    class CallbackRegistration:
        needs = (Sink,)

        def mount(self, ctx, callback):
            callbacks = ctx[Sink].callbacks
            callbacks.append(callback)
            ctx.effect(lambda: callbacks.remove(callback))

    def callback(fn):
        fn._harnessed_registrations = (CallbackRegistration(),)
        return fn

    class Feature(Plugin):
        @callback
        def answer(self):
            return 42

    async with Context() as ctx:
        sink = Sink()
        remove = ctx.provide(Sink, sink)
        handle = await ctx.plugin(Feature())
        assert sink.callbacks[0]() == 42
        await remove()
        await ctx.settle()
        assert sink.callbacks == [] and handle.status == "waiting"
