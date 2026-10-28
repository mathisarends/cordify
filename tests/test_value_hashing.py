"""Frozen mapping payloads have an explicit, predictable hashing contract."""

from collections.abc import Hashable

import pytest

from harnessed.core.messages import Message, ToolCall
from harnessed.core.model import ModelInfo, ModelRequest, ModelResponse, ModelUsage, ToolDefinition
from harnessed.core.session.events import ModelRequested, TurnStart


@pytest.mark.parametrize(
    "value",
    [
        ToolCall("read", {"path": "file"}),
        Message("user", "hello"),
        ToolDefinition("read", "Read", {"type": "object"}),
        ModelRequest("test", ()),
        ModelResponse(Message("assistant", "done")),
        TurnStart("one"),
        ModelRequested(ModelRequest("test", ())),
    ],
)
def test_mapping_values_and_log_records_are_explicitly_unhashable(value):
    assert not isinstance(value, Hashable)
    with pytest.raises(TypeError):
        hash(value)


def test_scalar_values_remain_hashable_and_payload_equality_remains_structural():
    assert hash(ModelInfo("test")) == hash(ModelInfo("test"))
    assert hash(ModelUsage(12, 3)) == hash(ModelUsage(12, 3))
    assert ToolCall("read", {"a": 1, "b": 2}, "one") == ToolCall("read", {"b": 2, "a": 1}, "one")
