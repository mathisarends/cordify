import asyncio
from contextlib import aclosing

from harnessed import Agent, events
from harnessed.models import ModelChunk
from harnessed.testing import ScriptedModel


async def main() -> None:
    model = ScriptedModel([[ModelChunk(text="The patch "), ModelChunk(text="looks good.")]])
    async with Agent(model=model).session() as session:
        async with aclosing(session.stream("Review the patch.")) as stream:
            async for event in stream:
                match event:
                    case events.TextDeltaEvent(text=text):
                        print(text, end="", flush=True)
                    case events.TurnFinishedEvent(result=result):
                        print("\nFinished:", result.stop_reason)


if __name__ == "__main__":
    asyncio.run(main())
