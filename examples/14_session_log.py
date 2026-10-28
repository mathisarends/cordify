"""Run with: uv run python -m examples.14_session_log"""

import asyncio
import json

from harnessed import Agent as Application
from harnessed import Bundle, ModelChunk, ToolCall, derive_messages, derive_requests
from harnessed.core.messages import User
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import AssistantMessage, UserMessage
from harnessed.core.session.session import SessionLog as Session
from harnessed.core.stream import TextDelta
from harnessed.plugins import Approvals
from harnessed.testing import ScriptedModel

math = Bundle("math")


@math.tool
async def add(a: int, b: int) -> int:
    return a + b


async def main():
    model = ScriptedModel(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 2, "b": 3}))],
            [ModelChunk(text="The result "), ModelChunk(text="is 5.")],
        ]
    )
    async with Application(model=model, plugins=[Approvals(trust="all"), math]).session(
        "source"
    ) as agent:
        async for event in agent.stream("Add 2 and 3"):
            if isinstance(event, TextDelta):
                print(event.text, end="", flush=True)
        print()
        log = agent.log.events
    encoded = json.dumps([encode_event(event) for event in log])
    restored = tuple(decode_event(record) for record in json.loads(encoded))
    replay = Session("replayed", seed=restored)
    assert replay.messages == derive_messages(log)
    assert replay.requests == derive_requests(log) == tuple(model.requests)
    print("Recorded requests:", len(replay.requests))
    print(
        "Replayed reply:",
        "".join(e.message.text for e in restored if isinstance(e, AssistantMessage)),
    )
    print("Last model history:", [(m.role, m.text) for m in replay.requests[-1].messages])
    fork = replay.fork("alternative")
    await fork.append(UserMessage(User("Explain the result.")))
    assert replay.events == restored
    print("Parent / fork message count:", len(replay.messages), len(fork.messages))


if __name__ == "__main__":
    asyncio.run(main())
