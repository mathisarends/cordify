import json
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ..seams.models import ImagePart, Message, ModelRequest, TextPart, to_dict

type NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
type Counter = Annotated[int, Field(ge=0)]
type PositiveInt = Annotated[int, Field(gt=0)]


class WireModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
        hide_input_in_errors=True,
    )


class DeepSeekConfig(WireModel):
    model: NonEmpty = "deepseek-flash"
    api_key: SecretStr | None = None
    base_url: AnyHttpUrl = AnyHttpUrl("https://api.deepseek.com")
    timeout: Annotated[float, Field(gt=0)] = 120.0

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def nonempty_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("API key must not be blank")
        return value


class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str


class ImageURL(WireModel):
    url: NonEmpty
    detail: Literal["auto", "low", "high", "original"] = "auto"


class ImageContent(WireModel):
    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


type ContentPart = Annotated[TextContent | ImageContent, Field(discriminator="type")]


class FunctionCall(WireModel):
    name: NonEmpty
    arguments: str


class FunctionToolCall(WireModel):
    id: NonEmpty
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(WireModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart]
    tool_call_id: NonEmpty | None = None
    reasoning_content: str | None = None
    tool_calls: list[FunctionToolCall] | None = None

    @model_validator(mode="after")
    def role_fields(self) -> Self:
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("Only tool messages require tool_call_id")
        if self.role != "assistant" and (self.tool_calls is not None or self.reasoning_content):
            raise ValueError("Only assistant messages may contain reasoning or tool calls")
        return self

    @classmethod
    def from_message(cls, message: Message) -> Self:
        content = message.content
        if not isinstance(content, str):
            content = [
                TextContent(text=part.text)
                if isinstance(part, TextPart)
                else ImageContent(image_url=ImageURL(url=part.url, detail=part.detail))
                for part in content
                if isinstance(part, TextPart | ImagePart)
            ]
        return cls(
            role=message.role,
            content=content,
            tool_call_id=message.tool_call_id,
            reasoning_content=message.reasoning_content or None,
            tool_calls=[
                FunctionToolCall(
                    id=call.id,
                    function=FunctionCall(
                        name=call.name,
                        arguments=json.dumps(to_dict(call.arguments), allow_nan=False),
                    ),
                )
                for call in message.tool_calls
            ]
            or None,
        )


class FunctionDefinition(WireModel):
    name: NonEmpty
    description: str = ""
    parameters: dict[str, JsonValue]


class FunctionTool(WireModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class StreamOptions(WireModel):
    include_usage: Literal[True] = True


class ChatCompletionRequest(WireModel):
    model: NonEmpty
    messages: Annotated[list[ChatMessage], Field(min_length=1)]
    stream: Literal[True] = True
    stream_options: StreamOptions = Field(default_factory=StreamOptions)
    tools: list[FunctionTool] | None = None
    max_tokens: PositiveInt | None = None

    @classmethod
    def from_request(cls, request: ModelRequest) -> Self:
        return cls(
            model=request.model,
            messages=[ChatMessage.from_message(message) for message in request.messages],
            tools=[
                FunctionTool(function=FunctionDefinition(**to_dict(tool))) for tool in request.tools
            ]
            or None,
            max_tokens=request.max_tokens,
        )


class ResponseModel(WireModel):
    # Providers may add metadata; known fields still undergo strict validation.
    model_config = ConfigDict(extra="ignore")


class FunctionDelta(ResponseModel):
    name: str | None = None
    arguments: str | None = None


class ToolCallDelta(ResponseModel):
    index: Counter
    id: str | None = None
    type: Literal["function"] | None = None
    function: FunctionDelta | None = None


class ChatDelta(ResponseModel):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCallDelta] | None = None


class StreamChoice(ResponseModel):
    index: Annotated[int, Field(ge=0, le=0)] = 0
    delta: ChatDelta
    finish_reason: str | None = None


class Usage(ResponseModel):
    prompt_tokens: Counter
    completion_tokens: Counter = 0
    prompt_cache_hit_tokens: Counter | None = None
    prompt_cache_miss_tokens: Counter | None = None


class ProviderError(ResponseModel):
    message: str
    type: str | None = None
    code: str | int | None = None


class ChatCompletionChunk(ResponseModel):
    choices: Annotated[list[StreamChoice], Field(max_length=1)] = Field(default_factory=list)
    usage: Usage | None = None
    error: ProviderError | None = None

    @model_validator(mode="after")
    def has_payload(self) -> Self:
        if self.error is None and "choices" not in self.model_fields_set:
            raise ValueError("Stream chunks require choices or an error")
        return self


TOOL_ARGUMENTS = TypeAdapter(dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False))
