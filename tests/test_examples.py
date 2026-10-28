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
        "examples.05_bundles_and_injection",
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
    ],
)
def test_offline_sdk_example_runs(module):
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
