from collections.abc import Hashable

import pytest

from harnessed.core.messages import (
    AssistantMessage,
    ImagePart,
    Observation,
    TextPart,
    ToolCall,
    UserMessage,
)
from harnessed.core.model import ModelInfo, ModelRequest, ModelResponse, ModelUsage, ToolDefinition
from harnessed.core.session.events import ModelRequestedEvent, TurnStartedEvent


@pytest.mark.parametrize(
    "value",
    [
        ToolCall("read", {"path": "file"}),
        UserMessage("hello"),
        ToolDefinition("read", "Read", {"type": "object"}),
        ModelRequest(()),
        ModelResponse(AssistantMessage("done")),
        TurnStartedEvent("one"),
        ModelRequestedEvent(ModelRequest(()), request_id="request", model=ModelInfo("test")),
    ],
)
def test_mapping_values_and_log_records_are_explicitly_unhashable(value):
    assert not isinstance(value, Hashable)
    with pytest.raises(TypeError):
        hash(value)


def test_observation_detaches_content_and_refs_from_mutable_capture_buffers():
    parts = [TextPart("page"), ImagePart("https://example.test/screen.png")]
    refs = ["frame"]
    snapshot = Observation("browser", parts, refs)
    parts.clear()
    refs.clear()
    assert snapshot.content == (TextPart("page"), ImagePart("https://example.test/screen.png"))
    assert snapshot.refs == ("frame",)


@pytest.mark.parametrize("content", [None, 42, ("plain text is not a content part",)])
def test_observation_rejects_unsupported_content(content):
    with pytest.raises(TypeError):
        Observation("browser", content)


def test_scalar_values_remain_hashable_and_payload_equality_remains_structural():
    assert hash(ModelInfo("test")) == hash(ModelInfo("test"))
    assert hash(ModelUsage(12, 3)) == hash(ModelUsage(12, 3))
    assert ToolCall("read", {"a": 1, "b": 2}, "one") == ToolCall("read", {"b": 2, "a": 1}, "one")
