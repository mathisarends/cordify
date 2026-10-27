from cordify.adapters.sse import iter_sse_data


async def lines(*items):
    for item in items:
        yield item


async def test_sse_joins_data_lines_and_ignores_comments_and_other_fields():
    frames = iter_sse_data(
        lines(
            "\ufeff: heartbeat",
            "event: message",
            "id: 42",
            'data: {"choices":',
            "data: []}",
            "",
            ": another heartbeat",
            "",
            "retry: 1000",
            "data:[DONE]",
            "",
        )
    )
    assert [frame async for frame in frames] == ['{"choices":\n[]}', "[DONE]"]


async def test_sse_preserves_whitespace_and_discards_unterminated_frames():
    frames = iter_sse_data(
        lines("\ufeffdata:  leading and trailing  ", "data", "", "data: truncated")
    )
    assert [frame async for frame in frames] == [" leading and trailing  \n"]


async def test_sse_emits_empty_data_frames_but_ignores_unknown_field_case():
    frames = iter_sse_data(lines("Data: ignored", "", "data:", "", "data", "data", ""))
    assert [frame async for frame in frames] == ["", "\n"]
