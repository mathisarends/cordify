"""Run offline; add --otlp to send to a configured OTLP/HTTP endpoint."""

import argparse
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from harnessed import Agent, Plugin, PolicyDecision, tool
from harnessed.integrations.opentelemetry import OpenTelemetryTracer, otlp_provider
from harnessed.models import ModelUsage
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel, call, reply


class Calculator(Plugin):
    @tool
    async def add(self, a: int, b: int) -> int:
        return a + b


@asynccontextmanager
async def console_provider() -> AsyncGenerator[TracerProvider]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    try:
        yield provider
    finally:
        await asyncio.to_thread(provider.shutdown)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Trace an offline scripted tool loop")
    parser.add_argument(
        "--otlp", action="store_true", help="Export via OTLP/HTTP instead of the offline console"
    )
    args = parser.parse_args()
    # Open once at application startup; close after every session using it.
    manager = (
        otlp_provider(
            service_name="harnessed-example",
            resource_attributes={"openinference.project.name": "harnessed-example"},
        )
        if args.otlp
        else console_provider()
    )
    async with manager as provider:
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
    print(
        "Completed tool loop; content capture is disabled. Check the exporter for delivery errors."
    )


if __name__ == "__main__":
    asyncio.run(main())
