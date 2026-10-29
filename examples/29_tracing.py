"""Run offline with: uv run --extra otel python -m examples.29_tracing"""

import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from harnessed import Agent, Plugin, PolicyDecision, tool
from harnessed.integrations.opentelemetry import OpenTelemetryTracer
from harnessed.models import ModelUsage
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel, call, reply


class Calculator(Plugin):
    @tool
    async def add(self, a: int, b: int) -> int:
        return a + b


async def main() -> None:
    # A backend creates this once during startup. Use BatchSpanProcessor and an
    # OTLPSpanExporter in production; the tracing guide has both backend configs.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    try:
        agent = Agent(
            model=ScriptedModel(
                [
                    call("add", a=19, b=23),
                    reply("42", usage=ModelUsage(20, 2)),
                ]
            ),
            plugins=[
                Calculator(),
                Approvals(default=PolicyDecision.ALLOW),
                OpenTelemetryTracer(tracer_provider=provider),
            ],
        )
        with provider.get_tracer("backend").start_as_current_span("backend.request"):
            result = await agent.run("Add 19 and 23")
        assert result.text == "42"
        print("Exported turn, step, model and tool spans; content capture is disabled.")
    finally:
        await asyncio.to_thread(provider.shutdown)


if __name__ == "__main__":
    asyncio.run(main())
