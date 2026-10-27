import os
import subprocess
import sys
from pathlib import Path

import pytest


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
        "examples.13_llm_adapters",
        "examples.14_session_log",
        "examples.15_tool_output_limits",
        "examples.16_compaction_policies",
    ],
)
def test_offline_sdk_example_runs(module):
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout
