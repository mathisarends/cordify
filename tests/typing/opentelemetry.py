from typing import assert_type

from opentelemetry.sdk.trace import TracerProvider

from harnessed import Agent
from harnessed.integrations.opentelemetry import OpenTelemetryTracer, otlp_provider


async def configure() -> None:
    async with otlp_provider(
        service_name="my-app",
        endpoint="http://localhost:6006/v1/traces",
        resource_attributes={"openinference.project.name": "my-agent", "worker.count": 2},
    ) as provider:
        assert_type(provider, TracerProvider)
        tracer = OpenTelemetryTracer(tracer_provider=provider, capture_content=False)
        assert_type(tracer, OpenTelemetryTracer)
        agent = Agent(plugins=[tracer])
        assert_type(agent, Agent)
