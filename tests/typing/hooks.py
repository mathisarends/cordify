from typing import assert_type

from harnessed.kernel import Bail, Context, Emit, Inject, PluginBase, Waterfall, hook
from harnessed.kernel.hooks import Next


class Notice(Emit):
    pass


class Other(Emit):
    pass


class Question(Bail[int]):
    pass


class Transform(Waterfall[str]):
    pass


@hook(Notice)
async def observe(event: Notice, dependency: Inject[int]) -> None:
    assert_type(dependency, int)


@hook(Question)
async def answer(event: Question) -> int | None:
    return None


@hook(Transform)
async def transform(event: Transform, next_: Next[str]) -> str:
    return await next_()


class Hooks(PluginBase, needs=(str,)):
    @hook(Notice)
    def observe(self, event: Notice) -> None:
        pass

    @hook(Question)
    async def answer(self, event: Question) -> int | None:
        return 42

    @hook(Transform)
    async def transform(self, event: Transform, next_: Next[str], value: Inject[str]) -> str:
        return value + await next_()


def wrong_event(event: Other) -> None:
    pass


def wrong_observer_result(event: Notice) -> int:
    return 42


async def wrong_answer(event: Question) -> str:
    return "wrong"


async def wrong_continuation(event: Transform, next_: Next[int]) -> str:
    return str(await next_())


async def wrong_transform_result(event: Transform, next_: Next[str]) -> int:
    return 42


async def consumer(ctx: Context) -> None:
    ctx.on(Notice, lambda event: None)
    ctx.on(Question, answer)
    ctx.on(Transform, transform)

    @ctx.hook(Transform)
    async def owned(event: Transform, next_: Next[str]) -> str:
        return await next_()

    async def terminal() -> str:
        return "end"

    assert_type(await transform(Transform(), terminal), str)
    assert_type(await owned(Transform(), terminal), str)
    assert_type(await Hooks().transform(Transform(), terminal, "prefix"), str)

    hook(Notice)(wrong_event)  # ty: ignore[no-matching-overload]
    hook(Notice)(wrong_observer_result)  # ty: ignore[no-matching-overload]
    hook(Question)(wrong_answer)  # ty: ignore[no-matching-overload]
    hook(Transform)(wrong_continuation)  # ty: ignore[no-matching-overload]
    hook(Transform)(wrong_transform_result)  # ty: ignore[no-matching-overload]
    ctx.hook(Notice)(wrong_event)  # ty: ignore[no-matching-overload]
    ctx.hook(Transform)(wrong_transform_result)  # ty: ignore[no-matching-overload]
    ctx.on(Notice, wrong_observer_result)  # ty: ignore[no-matching-overload]
    ctx.on(Transform, wrong_continuation)  # ty: ignore[no-matching-overload]
    ctx.add_listener(Question, wrong_answer)  # ty: ignore[invalid-argument-type]


class InvalidHooks(PluginBase):
    @hook(Notice)  # ty: ignore[no-matching-overload]
    async def wrong_event(self, event: Other) -> None:
        pass

    @hook(Transform)  # ty: ignore[no-matching-overload]
    async def wrong_result(self, event: Transform, next_: Next[str]) -> int:
        return 42
