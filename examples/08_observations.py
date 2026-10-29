"""Run with: uv run python -m examples.08_observations (uses an in-memory browser)."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from harnessed import Agent, Observation, Plugin, PolicyDecision, Session, ToolCall, tool
from harnessed.hooks import PreStep
from harnessed.kernel import Context, Inject, hook, plugin
from harnessed.models import ModelChunk
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel


@dataclass(frozen=True)
class PageState:
    url: str
    title: str

    def render(self) -> str:
        return f"Page: {self.title} ({self.url})\n[settings] button Settings\n[home] link Home"


class Browser(Protocol):
    @property
    def current_url(self) -> str: ...

    async def snapshot(self) -> PageState: ...

    async def click(self, ref: str) -> None: ...


class DemoBrowser:
    def __init__(self):
        self.current_url = "https://example.test/"
        self.title = "Home"

    async def snapshot(self) -> PageState:
        return PageState(self.current_url, self.title)

    async def click(self, ref: str):
        if ref != "settings":
            raise ValueError(f"Unknown ref: {ref}")
        self.current_url = "https://example.test/settings"
        self.title = "Settings"


@plugin
async def browser_backend(ctx: Context):
    ctx.provide(Browser, DemoBrowser())
    yield


class BrowserObserver(Plugin, needs=(Browser, Session)):
    @hook(PreStep)
    async def observe(self, event, next_, browser: Inject[Browser], session: Inject[Session]):
        state = await browser.snapshot()
        session.observe(
            Observation(
                source="browser",
                full=state.render(),
                summary=f"[page state: {state.url} - {state.title}]",
            )
        )
        return await next_()


class BrowserTools(Plugin):
    needs = (Browser,)

    @tool
    async def click(self, ref: str, browser: Inject[Browser]) -> str:
        """Click an element by its ref from the current page state."""
        before = browser.current_url
        await browser.click(ref)
        after = browser.current_url
        return f"Clicked {ref}." + (f" Navigated to {after}." if after != before else "")

    @tool
    async def snapshot(self, browser: Inject[Browser]) -> str:
        """Get the full current page state if the page may have changed."""
        return (await browser.snapshot()).render()


browser_tools = BrowserTools()


async def main():
    model = ScriptedModel(
        [[ModelChunk(tool_call=ToolCall("click", {"ref": "settings"}))], "Settings opened."]
    )
    agent = Agent(
        model=model,
        plugins=[
            Approvals(default=PolicyDecision.ALLOW),
            browser_backend(),
            BrowserObserver(),
            browser_tools,
        ],
    )
    async with agent.session("browser") as session:
        print((await session.run("Open Settings.")).text)
        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.id.startswith("observation:"):
                    print(message.text)


if __name__ == "__main__":
    asyncio.run(main())
