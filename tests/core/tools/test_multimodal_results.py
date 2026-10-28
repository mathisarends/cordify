"""Mixed tool results remain lossless across policies, history and replay."""

import json

import pytest

from harnessed import Agent, Plugin
from harnessed.core.messages import ImagePart, TextPart, ToolReply, ToolResult
from harnessed.core.model import ModelInfo, ModelRequest
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.session import SessionLog
from harnessed.core.tokens import ApproximateTokenCounter, count_content
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.plugins import Rules, when
from harnessed.plugins.spill import Spill, SpillStore
from harnessed.testing import ScriptedModel, call

IMAGE = ImagePart("data:image/png;base64,aW1hZ2U=", detail="high")
TEXT = "large tool output " * 500


@pytest.mark.parametrize("policy", ["plain", "limit", "spill"])
async def test_multimodal_tool_loop_retains_images_and_metadata_through_replay(tmp_path, policy):
    class Screenshot(Plugin):
        @tool(readonly=True)
        def screenshot(self) -> ToolResult:
            return ToolResult((TextPart(TEXT), IMAGE), exit_code=0)

    plugins = [Screenshot(), Rules([when(tool="screenshot").note("Keep the image.")])]
    if policy == "limit":
        plugins.append(ToolOutputLimit(max_chars=256))
    elif policy == "spill":
        plugins.append(Spill(threshold_tokens=32, preview_tokens=4))
    model = ScriptedModel(
        [call("screenshot"), "I can see it."],
        info=ModelInfo("vision", context_window=128_000, supports_images=True),
    )
    async with Agent(model=model, plugins=plugins).session() as session:
        result = await session.run("Look at the screenshot.")
        tool_result = result.tool_calls[0][1]
        assert tool_result.exit_code == 0 and tool_result.rule_matches
        assert [p for p in tool_result.content if isinstance(p, ImagePart)] == [IMAGE]
        history = next(m for m in model.requests[1].messages if m.role == "tool")
        assert history.content == tool_result.content
        assert history.status == tool_result.status and history.exit_code == 0
        records = json.loads(json.dumps([encode_event(e) for e in session.events]))
        replay = SessionLog("replay", seed=[decode_event(e) for e in records])
        assert replay.messages == session.messages
        if policy == "limit":
            assert tool_result.truncation and len(tool_result.text) <= 256
            assert ToolOutputLimit(256).apply(tool_result) is tool_result
        elif policy == "spill":
            assert tool_result.spill
            full = await session.ctx[SpillStore].read(tool_result.spill.path, limit=16000)
            assert TEXT in full and "Keep the image." in tool_result.text
        else:
            assert "Keep the image." in tool_result.text
        counter = ApproximateTokenCounter()
        assert (
            count_content(counter, tool_result.content)
            == counter.count_text(tool_result.text) + 1024
        )
        without_images = ToolReply(history.text, tool_call_id=history.tool_call_id)
        assert counter.count_request(ModelRequest("vision", (history,))) == (
            counter.count_request(ModelRequest("vision", (without_images,))) + 1024
        )


def test_notes_preserve_interleaved_text_and_images_and_detach_mutable_sequences():
    pieces = [TextPart("before"), IMAGE, TextPart("after")]
    result = ToolResult(pieces)
    pieces.clear()
    noted = result.with_note("note")
    assert noted.content == (*result.content, TextPart("note"))
    assert noted.with_text("replacement").text == "replacement"
    assert IMAGE in noted.with_text("replacement").content
