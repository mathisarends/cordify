from datetime import timedelta

import pytest

from harnessed.core.messages import ToolCall
from harnessed.core.prompt import PromptSectionConflict, SystemPrompt
from harnessed.core.tools import ToolError, ToolNameConflict, ToolRegistry
from harnessed.kernel import Context


async def test_explicit_owner_controls_tool_and_prompt_lifetime():
    async with Context() as root:
        owner = root.extend()
        registry, prompt = ToolRegistry(root), SystemPrompt(root)

        @registry.tool(owner=owner)
        async def selected() -> str:
            return "selected"

        prompt.section("owned", "selected", owner=owner)
        assert registry.definitions and await prompt.render() == "selected"
        await owner.aclose()
        assert registry.definitions == () and await prompt.render() == ""


async def test_duplicate_tool_names_fail_unless_override_is_explicit():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        def original() -> str:
            return "original"

        registry.register(original, name="lookup")
        with pytest.raises(ToolNameConflict, match="override=True"):
            registry.register(original, name="lookup")
        remove = registry.register(lambda: "replacement", name="lookup", override=True)
        assert (await registry.execute(ToolCall("lookup"))).content == "replacement"
        await remove()
        assert (await registry.execute(ToolCall("lookup"))).content == "original"


async def test_tool_errors_redact_secrets_but_preserve_explicit_safe_messages(caplog):
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        def broken() -> str:
            raise RuntimeError("postgres://secret:password@internal/database")

        @registry.tool
        def expected() -> str:
            raise ToolError("Choose an existing project.")

        assert (await registry.execute(ToolCall("broken"))).content == "broken failed: RuntimeError"
        assert (
            await registry.execute(ToolCall("expected"))
        ).content == "Choose an existing project."
        assert "postgres://secret" in caplog.text


async def test_prompt_conflicts_invalidation_and_expiry(monkeypatch):
    import harnessed.core.prompt as prompt_module

    clock = [1.0]
    monkeypatch.setattr(prompt_module, "monotonic", lambda: clock[0])
    async with Context() as ctx:
        prompt = SystemPrompt(ctx)
        current = ["one"]
        prompt.section("cached", lambda: current[0], stable_for=timedelta(seconds=10))
        assert await prompt.render() == "one"
        with pytest.raises(PromptSectionConflict):
            prompt.section("cached", "oops")
        current[0] = "two"
        assert await prompt.render() == "one"
        clock[0] = 12
        assert await prompt.render() == "two"
        current[0] = "three"
        prompt.invalidate("cached")
        assert await prompt.render() == "three"
