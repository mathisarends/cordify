from harnessed import Agent
from harnessed.core.model import ModelChunk, ModelUsage, sum_usage
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import ModelUsageRecorded
from harnessed.core.session.session import SessionLog as Session
from harnessed.testing import ScriptedModel


def test_cache_hit_rate_and_optional_totals_use_canonical_counters():
    usage = ModelUsage(100, 10, cache_read_tokens=80, cache_write_tokens=5, reasoning_tokens=4)
    assert usage.cache_hit_rate == 0.8
    assert decode_event(encode_event(ModelUsageRecorded(usage))).usage == usage
    combined = sum_usage([usage, ModelUsage(20, 2)])
    assert combined.total_tokens == 132 and combined.cache_read_tokens is None


async def test_final_usage_is_recorded_once_and_session_totals_survive_replay():
    partial, final = ModelUsage(10, 2), ModelUsage(10, 5)
    model = ScriptedModel(
        [
            [ModelChunk(text="first", usage=partial), ModelChunk(usage=final)],
            [ModelChunk(text="second", usage=ModelUsage(20, 3))],
        ]
    )
    async with Agent(model=model).session() as session:
        first = await session.run("Go")
        assert first.usage == final
        await session.run("Again")
        recorded = [event for event in session.events if isinstance(event, ModelUsageRecorded)]
        assert len(recorded) == 2
        assert session.usage.input_tokens == 30 and session.usage.output_tokens == 8
        assert Session("replay", seed=session.events).usage == session.usage
