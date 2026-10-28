import json
from pathlib import Path

from harnessed import (
    FS,
    Bundle,
    File,
    Harness,
    Image,
    ImagePart,
    Inject,
    Message,
    ModelChunk,
    Observation,
    Plugin,
    PreStep,
    SystemPrompt,
    ToolCall,
    hook,
    system,
    user,
)
from harnessed import (
    AgentHandle as Agent,
)
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import ContextMessage, ModelRequested, ObservationRecorded
from harnessed.core.session.session import Session
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.filesystem import fs_local
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedLLM


async def test_task_context_lasts_one_turn_and_precedes_task_prompt():
    tools = Bundle("continue")

    @tools.tool
    async def step() -> str:
        return "done"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("step"))], "first", "second"])
    async with Harness([agent_loop(), llm(model), tools]).session("context") as agent:
        await agent.run("review", context={"ticket": "Fix login", "details": {"issue": 42}})
        for request in model.requests[:2]:
            assert "Task context [ticket]:\nFix login" in [m.text for m in request.messages]
            assert request.messages[0].role == "system"
            assert request.messages[2].text == "review"
        await agent.run("unrelated")
        assert all("Task context" not in m.text for m in model.requests[-1].messages)
        assert any(isinstance(e, ContextMessage) for e in agent.session.events)
        assert "Fix login" in json.dumps([encode_event(e) for e in agent.session.events])


async def test_file_and_image_attachments_are_snapshotted_in_log_and_wire_payload(tmp_path):
    diff = tmp_path / "PR.diff"
    diff.write_text("+ fixed login", encoding="utf-8")
    image = tmp_path / "screenshot.png"
    image.write_bytes(b"image-test-bytes")
    model = ScriptedLLM(["reviewed"])
    async with Harness([agent_loop(), llm(model)]).session("attachments") as agent:
        await agent.run("Review", attachments=[File(diff), Image(image)])
        request = model.requests[0]
        message = request.messages[0]
        assert "File: PR.diff\n+ fixed login" in message.text
        assert any(isinstance(part, ImagePart) for part in message.content)
        image_part = next(part for part in message.content if isinstance(part, ImagePart))
        assert image_part.url.startswith("data:image/png;")
        diff.write_text("changed after admission", encoding="utf-8")
        restored = tuple(
            decode_event(json.loads(json.dumps(encode_event(e)))) for e in agent.session.events
        )
        recorded = next(e.request for e in restored if isinstance(e, ModelRequested))
        assert recorded == request and "changed after admission" not in recorded.messages[0].text


async def test_system_injection_survives_prompt_updates_and_typed_user_steering():
    model = ScriptedLLM(["first", "second"])
    async with Harness([agent_loop(), llm(model)]).session("typed") as agent:
        await agent.inject(system("External change"))
        await agent.steer(user("Check auth"))
        await agent.when_idle()
        assert [(m.role, m.text) for m in model.requests[0].messages] == [
            ("system", "External change"),
            ("user", "Check auth"),
        ]
        agent.ctx[SystemPrompt].section("new", "Updated base prompt")
        await agent.run("another turn")
        assert any(
            m.role == "system" and m.text == "External change" for m in model.requests[-1].messages
        )


async def test_push_context_from_pre_step_is_in_the_current_request():
    seen = []

    class Recall(Plugin):
        @hook(PreStep)
        async def recall(self, event, next_, agent: Inject[Agent]):
            seen.append((event.is_turn_start, event.latest_user_text))
            if event.is_turn_start:
                await agent.inject(system("Relevant memory: prefers Python"))
            return await next_()

    model = ScriptedLLM(["done"])
    async with Harness([agent_loop(), llm(model), Recall()]).session("push") as agent:
        await agent.run("my task")
        assert seen == [(True, "my task")]
        assert len(model.requests) == 1
        assert model.requests[0].messages[-1].text == "Relevant memory: prefers Python"
        assert not agent.inbox.pending


async def test_injection_only_class_lifecycle_and_dynamic_project_context(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("first rules", encoding="utf-8")

    class Project(Plugin):
        async def setup(self, prompt: Inject[SystemPrompt], fs: Inject[FS]):
            prompt.section("project", lambda: fs.read(Path("AGENTS.md")), stable=False)

    model = ScriptedLLM(["one", "two"])
    async with Harness([agent_loop(), llm(model), fs_local(tmp_path), Project()]).session(
        "project"
    ) as agent:
        await agent.run("one")
        instructions.write_text("second rules", encoding="utf-8")
        await agent.run("two")
        assert model.requests[0].messages[0].text == "first rules"
        assert model.requests[1].messages[0].text == "second rules"


async def test_observations_use_full_current_state_and_summaries_in_later_steps_and_replay():
    tools = Bundle("browser")
    states = iter(["before", "after"])

    class BrowserObserver(Plugin):
        @hook(PreStep)
        async def observe(self, event, next_, agent: Inject[Agent]):
            state = next(states)
            agent.observe(Observation("browser", f"FULL {state} refs", f"SUMMARY {state}"))
            return await next_()

    @tools.tool
    async def click(ref: str) -> str:
        return f"Clicked {ref}"

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("click", {"ref": "button-1"}))], "done"])
    async with Harness([agent_loop(), llm(model), tools, BrowserObserver()]).session(
        "browser"
    ) as agent:
        await agent.run("Open settings")
        assert model.requests[0].messages[-1].text == "FULL before refs"
        texts = [m.text for m in model.requests[1].messages]
        assert "SUMMARY before" in texts and "FULL before refs" not in texts
        assert texts[-1] == "FULL after refs"
        assert any(isinstance(e, ObservationRecorded) for e in agent.session.events)
        replay = Session("replay", seed=agent.session.events)
        assert replay.messages == agent.session.messages
        assert all(not m.text.startswith("FULL") for m in replay.messages)
        fork = agent.session.fork("fork")
        assert fork.messages == replay.messages


async def test_remote_images_are_references_without_download():
    parts = await Image("https://example.com/screenshot.png", detail="low").parts()
    assert parts == (ImagePart("https://example.com/screenshot.png", "low"),)
    message = Message("user", list(parts))
    assert isinstance(message.content, tuple)
