from __future__ import annotations

from dataclasses import replace

from harnessed.core.errors import ConfigError
from harnessed.core.messages import Assistant, ToolResult, User
from harnessed.core.model import (
    Model,
    ModelRequest,
    OutputMode,
    OutputSchema,
    StopReason,
    TokenLimitBehavior,
    ToolDefinition,
)
from harnessed.core.outputs import FINAL_ANSWER, OutputTypes, OutputValues, validation_note
from harnessed.core.runtime.options import AgentOptions
from harnessed.core.session.events import (
    AssistantMessage,
    ContextLifetime,
    ContextMessage,
    OutputValidated,
    OutputValidationFailed,
)
from harnessed.core.session.session import SessionLog
from harnessed.kernel import Context


class OutputHandler:
    def __init__(
        self, ctx: Context, log: SessionLog, options: AgentOptions, values: OutputValues
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.options = options
        self.values = values

    def prepare(self, request: ModelRequest, schema: OutputSchema | None) -> ModelRequest:
        """Expose either native JSON output or one reserved, side-effect-free answer tool."""
        if schema is None:
            return request
        self.ctx[OutputTypes].resolve(schema)
        if any(tool.name == FINAL_ANSWER for tool in request.tools):
            raise ConfigError("The tool name 'final_answer' is reserved for structured output")
        mode = (
            OutputMode.NATIVE
            if self.ctx[Model].info.supports_structured_output
            else OutputMode.TOOL
        )
        schema = replace(schema, mode=mode)
        tools = request.tools
        if mode == OutputMode.TOOL:
            tools = (
                *tools,
                ToolDefinition(
                    FINAL_ANSWER,
                    "Return the final answer after completing any other tools.",
                    schema.json_schema,
                ),
            )
        return replace(request, tools=tools, output_schema=schema)

    async def validate(
        self, response: Assistant, schema: OutputSchema | None
    ) -> tuple[dict[str, ToolResult], str | None]:
        """Commit the verdict before control-call pairs; return a bounded terminal failure."""
        if schema is None:
            return {}, None
        current = self.log.open_turn
        assert current is not None
        outputs = [call for call in response.tool_calls if call.name == FINAL_ANSWER]
        ordinary = [call for call in response.tool_calls if call.name != FINAL_ANSWER]
        if not outputs and ordinary:
            return {}, None
        answer = self.log.last(AssistantMessage)
        assert answer is not None
        if not outputs and (
            answer.stop_reason in (StopReason.CONTENT_FILTER, StopReason.ERROR)
            or answer.stop_reason == StopReason.MAX_TOKENS
            and self.options.on_max_tokens != TokenLimitBehavior.CONTINUE
        ):
            return {}, None
        compiled = self.ctx[OutputTypes].resolve(schema)
        try:
            if ordinary or len(outputs) > 1:
                raise ValueError("Finish application tools before returning one final answer")
            if outputs and outputs[0].malformed is not None:
                raise ValueError("Tool arguments must be a valid JSON object")
            if schema.mode == OutputMode.TOOL and not outputs:
                raise ValueError("Use final_answer to return the declared response")
            value, data = compiled.parse(dict(outputs[0].arguments) if outputs else response.text)
        except Exception as error:
            note = str(error) if ordinary or len(outputs) > 1 else validation_note(error)
            failures = {
                e.message_id for e in current.events if isinstance(e, OutputValidationFailed)
            }
            attempt = len(failures) + 1
            for call in outputs or [None]:
                await self.log.append(
                    OutputValidationFailed(
                        current.id,
                        schema.id,
                        response.id,
                        attempt,
                        note,
                        call.id if call is not None else None,
                    )
                )
            if not outputs:
                await self.log.append(ContextMessage(User(note), ContextLifetime.TURN, current.id))
            return (
                {call.id: ToolResult.error(note) for call in outputs},
                note if attempt > self.options.output_retries else None,
            )
        proof = OutputValidated(
            current.id, schema.id, response.id, data, outputs[0].id if outputs else None
        )
        try:
            await self.log.append(proof)
        finally:
            # Persistence acknowledges before listener delivery, even if the caller is cancelled.
            if self.log.last(OutputValidated) is proof:
                self.values.put(proof.id, schema.id, value)
        return {call.id: ToolResult("Structured answer accepted.") for call in outputs}, None
