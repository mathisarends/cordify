from collections.abc import AsyncIterable, AsyncIterator


async def iter_sse_data(lines: AsyncIterable[str]) -> AsyncIterator[str]:
    """Yield complete SSE data frames from decoded lines without line endings."""
    data: list[str] = []
    first = True
    async for line in lines:
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
