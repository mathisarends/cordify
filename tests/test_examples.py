import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_RUN_EXAMPLE = """
import faulthandler
import runpy
import sys

module = sys.argv.pop(1)
faulthandler.dump_traceback_later(15, repeat=True)
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


@pytest.mark.parametrize(
    "module",
    [
        "examples.01_tool_agent",
        "examples.02_plugins",
        "examples.03_inbox",
        "examples.05_tools_and_injection",
        "examples.06_explicit_registration",
        "examples.07_context",
        "examples.08_observations",
        "examples.09_rules",
        "examples.10_context_strategies",
        "examples.11_temporal_needs",
        "examples.12_layered_sdk",
        "examples.13_models",
        "examples.14_session_log",
        "examples.15_tool_output_limits",
        "examples.16_compaction_policies",
        "examples.17_resume_crash",
        "examples.18_parallel_tools",
        "examples.19_approvals_and_priorities",
        "examples.20_pause_resume_stop",
        "examples.21_validated_forms",
        "examples.22_builtin_tool_lifetimes",
        "examples.23_plugin_lifecycle",
        "examples.24_session_spill",
        "examples.25_structured_output",
        "examples.26_ui_stream",
        "examples.27_skills",
        "examples.28_custom_events",
        "examples.29_tracing",
        "examples.30_conditional_tools",
        "examples.31_mcp",
        "examples.32_system_prompts",
        "examples.33_basic_stream",
        "examples.34_sandbox",
    ],
)
def test_offline_sdk_example_runs(module):
    if module == "examples.29_tracing":
        pytest.importorskip("opentelemetry.sdk")
    if module == "examples.31_mcp":
        pytest.importorskip("mcp")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _RUN_EXAMPLE, module],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except subprocess.TimeoutExpired as error:
        output = error.stderr or b""
        details = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
        pytest.fail(f"{module} timed out; child traceback:\n{details[-8000:]}", pytrace=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout


# Names without a public facade yet. Shrink this, never grow it.
_INTERNAL_EXAMPLE_IMPORTS = {
    ("harnessed.core.prompt.system_prompt", "SystemPromptStore"),
    ("harnessed.core.session.codec", "decode_event"),
    ("harnessed.core.session.codec", "encode_event"),
    ("harnessed.core.session.session", "SessionLog"),
    ("harnessed.core.session.session", "derive_messages"),
    ("harnessed.core.session.session", "derive_requests"),
    ("harnessed.core.tools.registry", "ToolRegistry"),
}


def test_examples_use_public_facades_except_for_known_gaps():
    root = Path(__file__).resolve().parents[1] / "examples"
    found = set()
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "harnessed.core"
            ):
                found.update((node.module, alias.name) for alias in node.names)
    assert found <= _INTERNAL_EXAMPLE_IMPORTS, found - _INTERNAL_EXAMPLE_IMPORTS
