import pytest

from harnessed.core.compaction import ModelSummarizer
from harnessed.core.messages import Assistant, ToolCall, User
from harnessed.core.model import ModelInfo, ModelResponse


class RequestModel:
    info = ModelInfo("session-model")

    def __init__(self, response):
        self.response = response
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        return self.response

    def stream(self, request):
        raise AssertionError("Summarization must use Model.request")


async def test_model_summarizer_uses_neutral_request_with_focus_and_no_tools():
    model = RequestModel(ModelResponse(Assistant("  Decisions and next steps.  ")))
    summarizer = ModelSummarizer(model, focus="Preserve failing test names.")
    summary = await summarizer.summarize((User("Fix tests/auth.py."),), max_tokens=32)
    assert summary == "Decisions and next steps."
    request = model.requests[0]
    assert request.model == "session-model" and request.max_tokens == 32
    assert not request.tools
    assert "Preserve failing test names." in request.messages[0].text
    assert "tests/auth.py" in request.messages[1].text


@pytest.mark.parametrize(
    "message, error",
    [(Assistant("  "), "empty summary"), (Assistant(tool_calls=(ToolCall("work"),)), "tool call")],
)
async def test_model_summarizer_rejects_invalid_summary_responses(message, error):
    model = RequestModel(ModelResponse(message))
    with pytest.raises(ValueError, match=error):
        await ModelSummarizer(model).summarize((User("history"),), max_tokens=32)
