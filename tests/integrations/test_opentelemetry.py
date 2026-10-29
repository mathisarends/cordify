import asyncio
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harnessed import Agent, ApprovalAnswer, Plugin, PolicyDecision, ToolCall, tool
from harnessed.core.hooks import SessionAppended
from harnessed.core.messages import Assistant, ToolResult, ToolStatus
from harnessed.core.model import ModelChunk, ModelUsage
from harnessed.core.session.events import AgentControlChangedEvent, ControlState
from harnessed.core.tracing import Tracer
from harnessed.integrations.opentelemetry import OpenTelemetryTracer
from harnessed.plugins import Approvals, Compaction, every_turns
from harnessed.testing import CallbackModel, ScriptedModel, reply


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


async def test_runtime_tree_usage_and_backend_attributes(telemetry):
    provider, exporter = telemetry
    usage = ModelUsage(20, 7, cache_read_tokens=4, reasoning_tokens=2, cost=0.01)
    model = ScriptedModel([reply("answer", usage=usage)])
    parent_tracer = provider.get_tracer("backend")
    with parent_tracer.start_as_current_span("http.request") as parent:
        await Agent(model=model, plugins=[OpenTelemetryTracer(tracer_provider=provider)]).run(
            "secret prompt"
        )
    spans = {span.name: span for span in exporter.get_finished_spans()}
    turn, step, model = [spans[f"harnessed.{name}"] for name in ("turn", "step", "model")]
    assert turn.parent.span_id == parent.get_span_context().span_id
    assert step.parent.span_id == turn.context.span_id
    assert model.parent.span_id == step.context.span_id
    assert {span.context.trace_id for span in spans.values()} == {turn.context.trace_id}
    assert model.attributes["openinference.span.kind"] == "LLM"
    assert model.attributes["langfuse.observation.type"] == "generation"
    assert model.attributes["llm.model_name"] == "scripted"
    assert model.attributes["llm.token_count.prompt"] == 20
    assert model.attributes["llm.token_count.completion"] == 7
    assert model.attributes["llm.token_count.total"] == 27
    assert model.attributes["gen_ai.usage.cost"] == 0.01
    assert model.attributes["harnessed.request_id"]
    assert turn.attributes["harnessed.outcome"] == "completed"
    for span in (turn, step, model):
        assert span.attributes["session.id"] == turn.attributes["session.id"]
        assert span.attributes["harnessed.turn_id"] == turn.attributes["harnessed.turn_id"]
        assert "input.value" not in span.attributes
        assert "output.value" not in span.attributes
    assert "secret prompt" not in str([span.attributes for span in spans.values()])
    assert trace.get_current_span().get_span_context().is_valid is False


async def test_content_is_explicit_bounded_and_excludes_opaque_state(telemetry):
    provider, exporter = telemetry
    native = Assistant("answer", reasoning="private reasoning", provider_state={"key": "opaque"})
    # Scripted streams retain response text; exercise the portable serializer
    # directly as well to check exclusion of opaque provider state.
    plugin = OpenTelemetryTracer(tracer_provider=provider, capture_content=True)
    async with Agent(model=ScriptedModel(["answer"]), plugins=[plugin]).session() as session:
        await session.run("secret prompt")
        tracer = session.ctx[Tracer]
        with tracer.span("model", session_id=session.id) as span:
            span.set_attribute("output", native)
    model_spans = [s for s in exporter.get_finished_spans() if s.name == "harnessed.model"]
    assert json.loads(model_spans[0].attributes["input.value"])[0]["content"] == "secret prompt"
    assert json.loads(model_spans[1].attributes["output.value"]) == {
        "role": "assistant",
        "content": "answer",
    }
    assert "opaque" not in str(model_spans[1].attributes)
    assert "private reasoning" not in str(model_spans[1].attributes)
    await Agent(
        model=ScriptedModel(["answer"]),
        plugins=[
            OpenTelemetryTracer(
                tracer_provider=provider, capture_content=True, max_content_length=8
            )
        ],
    ).run("long prompt")
    model = [s for s in exporter.get_finished_spans() if s.name == "harnessed.model"][-1]
    assert len(model.attributes["input.value"]) == 8
    assert model.attributes["harnessed.input.truncated"] is True
    assert model.attributes["input.mime_type"] == "text/plain"


async def test_parallel_tools_are_siblings_and_inherit_context(telemetry):
    provider, exporter = telemetry
    barrier = asyncio.Barrier(2)

    class Work(Plugin):
        @tool(parallel=True)
        async def work(self, index: int) -> str:
            await barrier.wait()
            with provider.get_tracer("application").start_as_current_span(f"inner.{index}"):
                return "done"

    calls = [ToolCall("work", {"index": i}) for i in range(2)]
    await Agent(
        model=ScriptedModel([[ModelChunk(tool_call=call) for call in calls], "done"]),
        plugins=[
            Work(),
            Approvals(default=PolicyDecision.ALLOW),
            OpenTelemetryTracer(tracer_provider=provider),
        ],
    ).run("go")
    spans = exporter.get_finished_spans()
    tools = [s for s in spans if s.name == "harnessed.tool"]
    assert len(tools) == 2
    assert tools[0].parent.span_id == tools[1].parent.span_id
    assert {s.attributes["harnessed.call_id"] for s in tools} == {c.id for c in calls}
    assert all(s.attributes["harnessed.step"] == 1 for s in tools)
    assert all(s.attributes["harnessed.tool.status"] == "ok" for s in tools)
    for index, call in enumerate(calls):
        inner = next(s for s in spans if s.name == f"inner.{index}")
        outer = next(s for s in tools if s.attributes["harnessed.call_id"] == call.id)
        assert inner.parent.span_id == outer.context.span_id


@pytest.mark.parametrize(
    ("status", "exit_code", "description"),
    [
        (ToolStatus.ERROR, None, "error"),
        (ToolStatus.TIMEOUT, None, "timeout"),
        (ToolStatus.UNAVAILABLE, None, "unavailable"),
        (ToolStatus.OK, 1, "nonzero_exit_code"),
    ],
)
async def test_tool_result_errors_mark_the_span_without_raising(
    telemetry, status, exit_code, description
):
    provider, exporter = telemetry

    class Work(Plugin):
        @tool
        def work(self) -> ToolResult:
            return ToolResult("private error details", status, exit_code=exit_code)

    await Agent(
        model=ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "recovered"]),
        plugins=[
            Work(),
            Approvals(default=PolicyDecision.ALLOW),
            OpenTelemetryTracer(tracer_provider=provider),
        ],
    ).run("go")
    tool_span = next(s for s in exporter.get_finished_spans() if s.name == "harnessed.tool")
    assert tool_span.status.status_code == trace.StatusCode.ERROR
    assert tool_span.attributes["harnessed.tool.status"] == status.value
    assert "private error details" not in str(tool_span.attributes)
    assert tool_span.status.description == description


async def test_approval_wait_is_separate_and_records_decision(telemetry):
    provider, exporter = telemetry

    class Work(Plugin):
        @tool
        def work(self) -> str:
            return "done"

    await Agent(
        model=ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "done"]),
        plugins=[
            Work(),
            Approvals(approver=lambda _: ApprovalAnswer.ALLOW),
            OpenTelemetryTracer(tracer_provider=provider),
        ],
    ).run("go")
    spans = exporter.get_finished_spans()
    approval = next(s for s in spans if s.name == "harnessed.approval_wait")
    tool_span = next(s for s in spans if s.name == "harnessed.tool")
    assert approval.end_time <= tool_span.start_time
    assert approval.attributes["harnessed.decision"] == "allow"
    assert approval.parent.span_id == tool_span.parent.span_id


async def test_failure_closes_spans_without_exporting_exception_text(telemetry):
    provider, exporter = telemetry

    async def fail(request):
        yield ModelChunk(text="partial secret", usage=ModelUsage(3, 1))
        raise RuntimeError("private exception")

    with pytest.raises(RuntimeError, match="private exception"):
        await Agent(
            model=CallbackModel(fail), plugins=[OpenTelemetryTracer(tracer_provider=provider)]
        ).run("secret prompt")
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["harnessed.model", "harnessed.step", "harnessed.turn"]
    assert all(s.status.status_code == trace.StatusCode.ERROR for s in spans)
    assert all("exception.message" not in s.events[0].attributes for s in spans)
    assert spans[0].attributes["llm.token_count.total"] == 4
    assert "partial secret" not in str(spans[0].attributes)


async def test_abort_closes_spans_and_preserves_partial_usage(telemetry):
    provider, exporter = telemetry
    started = asyncio.Event()

    async def wait(request):
        yield ModelChunk(text="partial", usage=ModelUsage(4, 2))
        started.set()
        await asyncio.Event().wait()

    async with Agent(
        model=CallbackModel(wait), plugins=[OpenTelemetryTracer(tracer_provider=provider)]
    ).session() as session:
        await session.submit("go")
        await started.wait()
        await session.abort()
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    assert all(s.attributes["harnessed.cancelled"] for s in spans)
    assert spans[0].attributes["llm.token_count.total"] == 6


async def test_graceful_stop_is_not_recorded_as_an_exception(telemetry):
    provider, exporter = telemetry
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        yield ModelChunk(text="finished step")

    async with Agent(
        model=CallbackModel(respond), plugins=[OpenTelemetryTracer(tracer_provider=provider)]
    ).session() as session:
        await session.submit("go")
        await started.wait()

        def release_on_stop(event: SessionAppended) -> None:
            if (
                isinstance(event.event, AgentControlChangedEvent)
                and event.event.state == ControlState.STOPPING
            ):
                release.set()

        session.on(SessionAppended, release_on_stop)
        await session.stop()
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    turn = next(s for s in spans if s.name == "harnessed.turn")
    assert turn.attributes["harnessed.outcome"] == "stopped"
    assert all(s.status.status_code != trace.StatusCode.ERROR for s in spans)
    assert all(not s.events for s in spans)


async def test_sessions_are_isolated_and_plugin_does_not_shutdown_provider(telemetry):
    provider, exporter = telemetry
    barrier = asyncio.Barrier(2)

    async def respond(request):
        await barrier.wait()
        yield ModelChunk(text="done")

    agent = Agent(
        model=CallbackModel(respond), plugins=[OpenTelemetryTracer(tracer_provider=provider)]
    )
    await asyncio.gather(agent.run("one"), agent.run("two"))
    turns = [s for s in exporter.get_finished_spans() if s.name == "harnessed.turn"]
    assert len(turns) == 2
    assert turns[0].context.trace_id != turns[1].context.trace_id
    assert turns[0].attributes["session.id"] != turns[1].attributes["session.id"]
    for span in exporter.get_finished_spans():
        turn = next(t for t in turns if t.context.trace_id == span.context.trace_id)
        assert span.attributes["session.id"] == turn.attributes["session.id"]
        assert span.attributes["harnessed.turn_id"] == turn.attributes["harnessed.turn_id"]
    with provider.get_tracer("backend").start_as_current_span("still.working"):
        pass
    assert exporter.get_finished_spans()[-1].name == "still.working"


async def test_compaction_span_records_its_usage(telemetry):
    provider, exporter = telemetry
    agent = Agent(
        model=ScriptedModel(["first", reply("summary", usage=ModelUsage(9, 2)), "second"]),
        plugins=[
            Compaction(trigger=every_turns(1), keep_turns=0),
            OpenTelemetryTracer(tracer_provider=provider),
        ],
    )
    async with agent.session() as session:
        await session.run("long prompt " * 100)
        await session.run("next")
    compact = next(s for s in exporter.get_finished_spans() if s.name == "harnessed.compaction")
    assert compact.attributes["llm.token_count.total"] == 11


def test_core_and_public_facades_import_without_otel():
    code = """
import importlib.abc
import sys
class NoOtel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('opentelemetry'):
            raise ImportError('optional dependency not installed')
sys.meta_path.insert(0, NoOtel())
import harnessed
import harnessed.plugins
import harnessed.models
import harnessed.integrations
from harnessed import Agent
from harnessed.core.tracing import NoopTracer
assert not any(n.startswith('opentelemetry') for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", ["/v1/traces", "/api/public/otel/v1/traces"])
async def test_real_otlp_http_export_to_local_collector(path):
    requests = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (self.path, self.headers, self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    provider = TracerProvider()
    endpoint = f"http://127.0.0.1:{server.server_port}{path}"
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, headers={"x-test": "harnessed"}, timeout=3)
        )
    )
    try:
        await Agent(
            model=ScriptedModel(["done"]), plugins=[OpenTelemetryTracer(tracer_provider=provider)]
        ).run("go")
        provider.force_flush()
        assert len(requests) == 3
        names = []
        for request_path, headers, body in requests:
            assert request_path == path
            assert headers["x-test"] == "harnessed"
            assert headers["Content-Type"] == "application/x-protobuf"
            data = ExportTraceServiceRequest.FromString(body)
            names.extend(
                s.name for r in data.resource_spans for scope in r.scope_spans for s in scope.spans
            )
        assert names == ["harnessed.model", "harnessed.step", "harnessed.turn"]
    finally:
        provider.shutdown()
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
