"""Run with: python -m examples.08_observations (uses an in-memory browser)."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from cordify import (
    Agent,
    Bundle,
    Context,
    Harness,
    Inject,
    ModelChunk,
    Observation,
    Plugin,
    PreStep,
    ToolCall,
    hook,
    plugin,
)
from cordify.std import agent_loop, llm
from cordify.testing import ScriptedLLM


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


class BrowserObserver(Plugin):
    @hook(PreStep)
    async def observe(self, event, next_, browser: Inject[Browser], agent: Inject[Agent]):
        state = await browser.snapshot()
        agent.observe(
            Observation(
                source="browser",
                full=state.render(),
                summary=f"[page state: {state.url} - {state.title}]",
            )
        )
        return await next_()


browser_tools = Bundle("browser")


@browser_tools.tool
async def click(ref: str, browser: Inject[Browser]) -> str:
    """Click an element by its ref from the current page state."""
    before = browser.current_url
    await browser.click(ref)
    after = browser.current_url
    return f"Clicked {ref}." + (f" Navigated to {after}." if after != before else "")


@browser_tools.tool
async def snapshot(browser: Inject[Browser]) -> str:
    """Get the full current page state if the page may have changed."""
    return (await browser.snapshot()).render()


async def main():
    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("click", {"ref": "settings"}))],
            "Settings opened.",
        ]
    )
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            browser_backend(),
            BrowserObserver(),
            browser_tools,
        ]
    )
    async with harness.session("browser") as agent:
        print((await agent.run("Open Settings.")).text)
        for index, request in enumerate(model.requests, 1):
            print(f"\nRequest {index}:")
            for message in request.messages:
                if message.id.startswith("observation:"):
                    print(message.text)


if __name__ == "__main__":
    asyncio.run(main())
