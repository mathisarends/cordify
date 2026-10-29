import pytest
from pydantic import TypeAdapter, ValidationError

from harnessed.core.approvals import ApprovalGate
from harnessed.core.budget import ContextBudget
from harnessed.core.model import ModelInfo, ModelRequest, ModelUsage
from harnessed.core.runtime.options import AgentLoopConfig
from harnessed.core.tools.registry import ToolRegistry
from harnessed.kernel import Context


@pytest.mark.parametrize(
    "options",
    [
        {"risky": "publish"},
        {"risky": {""}},
        {"risky": {"publish"}, "scope": ""},
        {"risky": {"publish"}, "priority": True},
        {"risky": {"publish"}, "ask": "allow"},
    ],
)
def test_scoped_guard_configuration_is_strict(options):
    with pytest.raises(ValidationError):
        ApprovalGate(**options)


@pytest.mark.parametrize(
    "options",
    [
        {"max_steps": True},
        {"max_steps": "2"},
        {"max_tokens": 0},
        {"context_window": -1},
        {"max_parallel_tools": 0},
        {"approval_timeout": float("inf")},
        {"on_max_tokens": "guess"},
    ],
)
def test_loop_options_are_strict_and_declarative(options):
    with pytest.raises(ValidationError):
        AgentLoopConfig(**options)


@pytest.mark.parametrize("options", [{"limit": 0}, {"reserve_tokens": -1}, {"limit": True}])
def test_context_budget_options_are_strict(options):
    with pytest.raises(ValidationError):
        ContextBudget(**options)


@pytest.mark.parametrize("timeout", [0, -1, True, "1", float("nan"), float("inf")])
def test_tool_timeouts_reject_invalid_values_at_both_registration_boundaries(timeout):
    with pytest.raises(ValidationError):
        ToolRegistry(Context(), default_timeout=timeout)
    registry = ToolRegistry(Context())
    with pytest.raises(ValidationError):
        registry.register(lambda: None, timeout=timeout)


def test_neutral_model_metadata_and_request_constraints_are_schema_visible():
    assert (
        TypeAdapter(ModelRequest).json_schema()["properties"]["max_tokens"]["anyOf"][0]["minimum"]
        == 1
    )
    for construct in (
        lambda: ModelInfo(""),
        lambda: ModelInfo("model", context_window=0),
        lambda: ModelInfo("model", supports_images="yes"),
        lambda: ModelRequest("model", (), max_tokens=True),
        lambda: ModelUsage(-1),
        lambda: ModelUsage(1, cost=float("nan")),
    ):
        with pytest.raises(ValidationError):
            construct()
