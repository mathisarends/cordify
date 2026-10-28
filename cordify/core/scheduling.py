import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass

from cordify.seams.models import ToolCall, ToolResult

type ResourceSpec = tuple[str, ...] | Callable[[ToolCall], Iterable[str]]


@dataclass(frozen=True)
class ToolScheduling:
    parallel: bool = False
    resources: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.parallel) is not bool:
            raise ValueError("parallel must be a bool")
        if isinstance(self.resources, str):
            raise TypeError("resources must be a tuple of resource names")
        keys = tuple(self.resources)
        if any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("Resource names must be non-empty strings")
        object.__setattr__(self, "resources", tuple(dict.fromkeys(keys)))


def resource_spec(resources: ResourceSpec) -> ResourceSpec:
    if callable(resources):
        if inspect.iscoroutinefunction(resources):
            raise TypeError("Resource resolvers must be synchronous")
        return resources
    return ToolScheduling(resources=resources).resources


@dataclass(frozen=True)
class ToolPlan:
    call: ToolCall
    scheduling: ToolScheduling
    error: ToolResult | None = None
    binding: object | None = None


class ToolScheduler:
    def __init__(self, max_parallel: int):
        self.max_parallel = max_parallel
        self.slots = asyncio.Semaphore(max_parallel)

    def groups(self, plans: Iterable[ToolPlan]) -> Iterator[tuple[ToolPlan, ...]]:
        pending = []
        for plan in plans:
            if self.max_parallel > 1 and plan.scheduling.parallel:
                pending.append(plan)
            else:
                if pending:
                    yield tuple(pending)
                    pending.clear()
                yield (plan,)
        if pending:
            yield tuple(pending)

    async def run(
        self,
        plans: tuple[ToolPlan, ...],
        worker: Callable[[int, ToolPlan], Awaitable[ToolResult]],
        commit: Callable[[ToolCall, ToolResult], Awaitable[None]],
    ) -> None:
        if len(plans) == 1:
            await commit(plans[0].call, await worker(0, plans[0]))
            return

        async def invoke(index, plan, predecessors):
            for predecessor in predecessors:
                await asyncio.shield(predecessor)
            return await worker(index, plan)

        tasks, resources = [], {}
        async with asyncio.TaskGroup() as group:
            for index, plan in enumerate(plans):
                predecessors = tuple(
                    dict.fromkeys(
                        resources[key] for key in plan.scheduling.resources if key in resources
                    )
                )
                task = group.create_task(
                    invoke(index, plan, predecessors), name=f"cordify:tool:{plan.call.id}"
                )
                tasks.append(task)
                for key in plan.scheduling.resources:
                    resources[key] = task
            for plan, task in zip(plans, tasks, strict=True):
                await commit(plan.call, await task)
