import pytest

from harnessed.core.errors import ToolNameConflict
from harnessed.core.messages import ToolCall
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.tools.registry import ToolError, ToolRegistry
from harnessed.kernel import Context


async def test_explicit_owner_controls_tool_and_prompt_lifetime():
    async with Context() as root:
        owner = root.extend()
        registry, prompt = ToolRegistry(root), SystemPrompt(root)

        @registry.tool(owner=owner)
        async def selected() -> str:
            return "selected"

        prompt.fixed("selected", owner=owner)
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
