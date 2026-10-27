"""Run with: python -m examples.14_session_log"""

import asyncio
import json

from cordify import Bundle, Message, ModelChunk, Session, ToolCall, derive_messages, derive_requests
from cordify.core.events import AssistantChunk, UserMessage
from cordify.core.session import decode_event, encode_event
from cordify.presets import minimal
from cordify.testing import ScriptedLLM

math = Bundle("math")


@math.tool
async def add(a: int, b: int) -> int:
    return a + b


async def main():
    model = ScriptedLLM(
        [
            [ModelChunk(tool_call=ToolCall("add", {"a": 2, "b": 3}))],
            [ModelChunk(text="The result "), ModelChunk(text="is 5.")],
        ]
    )
    async with minimal(model, plugins=[math]).session("source") as agent:
        async for event in agent.stream("Add 2 and 3"):
            if isinstance(event, AssistantChunk):
                print(event.text, end="", flush=True)
        print()
        log = agent.session.events

    # Replay reads only serialized events: no model, tool, Context or plugin setup.
    encoded = json.dumps([encode_event(event) for event in log])
    restored = tuple(decode_event(record) for record in json.loads(encoded))
    replay = Session("replayed", seed=restored)
    assert replay.messages == derive_messages(log)
    assert replay.requests == derive_requests(log) == tuple(model.requests)
    print("Recorded requests:", len(replay.requests))
    print("Replayed chunks:", "".join(e.text for e in restored if isinstance(e, AssistantChunk)))

    # Request snapshots include the exact messages and tool schemas used at each step.
    print("Last model history:", [(m.role, m.text) for m in replay.requests[-1].messages])

    # A fork shares an immutable prefix and owns its subsequent appends.
    fork = replay.fork("alternative")
    await fork.append(UserMessage(Message("user", "Explain the result.")))
    assert replay.events == restored
    print("Parent / fork message count:", len(replay.messages), len(fork.messages))


if __name__ == "__main__":
    asyncio.run(main())
