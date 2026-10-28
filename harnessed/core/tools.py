import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, cast, overload

from pydantic import ValidationError

from harnessed.core.messages import ToolCall, ToolResult, result_content, to_dict
from harnessed.core.model import ToolDefinition
from harnessed.kernel.context import Context
from harnessed.kernel.di import BindingPlan, binding_plan
from harnessed.kernel.effects import Disposer, run_sync
from harnessed.seams.shell import ProcResult

from .execution import ToolCallContext
from .scheduling import ResourceSpec, ToolPlan, ToolScheduling, resource_spec
from .validation import ToolArguments, tool_result_content

log = logging.getLogger(__name__)

_UNSPECIFIED = object()


class ToolNameConflict(ValueError):
    """Two live registrations claim the same name without an explicit override."""


class ToolError(Exception):
    """An intentional, safe error message that may be shown to the model."""


type ErrorFormatter = Callable[[ToolCall, Exception], str]


def format_tool_error(call: ToolCall, error: Exception, formatter: ErrorFormatter | None = None):
    log.error("Tool %s failed", call.name, exc_info=error)
    if formatter is not None:
        return formatter(call, error)
    if isinstance(error, ToolError):
        return str(error)
    if isinstance(error, ValidationError):
        return f"ValidationError: Invalid arguments for {call.name}: " + str(error)
    return f"{call.name} failed: {type(error).__name__}"


@dataclass(eq=False)
class _Tool:
    definition: ToolDefinition
    callback: Callable
    owner: Context
    plan: BindingPlan
    timeout: float | None
    parallel: bool
    resources: ResourceSpec
    arguments: ToolArguments
    readonly: bool = False


class ToolRegistry:
    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._tools: dict[str, list[_Tool]] = {}

    @overload
    def tool[F: Callable[..., Any]](self, fn: F, /) -> F: ...

    @overload
    def tool[F: Callable[..., Any]](
        self,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
        owner: Context | None = None,
    ) -> Callable[[F], F]: ...

    def tool(
        self,
        fn=None,
        /,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
        owner: Context | None = None,
    ):
        def register(callback):
            self.register(
                callback,
                name=name,
                timeout=timeout,
                parallel=parallel,
                resources=resources,
                readonly=readonly,
                override=override,
                owner=owner,
            )
            return callback

        return register(fn) if fn is not None else register

    def register(
        self,
        callback: Callable,
        *,
        name: str | None = None,
        timeout: float | None = None,
        parallel: bool = False,
        resources: ResourceSpec = (),
        readonly: bool = False,
        override: bool = False,
        owner: Context | None = None,
    ) -> Disposer:
        owner = owner if owner is not None else Context.current() or self._ctx
        owner._check_open()
        ToolScheduling(parallel)
        resources = resource_spec(resources)
        if timeout is not None and timeout <= 0:
            raise ValueError("Tool timeout must be positive")
        plan = binding_plan(callback)
        tool_name = name or getattr(callback, "__name__", type(callback).__name__)
        existing = self._tools.get(tool_name, ())
        if existing and not override:
            previous = existing[-1].owner
            raise ToolNameConflict(
                f"Tool {tool_name!r} is already registered by {self._owner_name(previous)}; "
                f"{self._owner_name(owner)} must declare override=True"
            )
        arguments = ToolArguments.from_signature(
            tool_name, plan.signature, plan.hints, plan.dependencies
        )
        definition = ToolDefinition(
            tool_name,
            inspect.getdoc(callback) or "",
            arguments.schema,
        )
        entry = _Tool(
            definition,
            callback,
            owner,
            plan,
            timeout,
            parallel,
            resources,
            arguments,
            readonly,
        )
        entries = self._tools.setdefault(definition.name, [])
        entries.append(entry)
        log.debug("Registered tool %s with owner %s", tool_name, self._owner_name(owner))

        def remove():
            entries.remove(entry)
            if not entries:
                del self._tools[definition.name]

        return owner.effect(remove)

    @staticmethod
    def _owner_name(owner: Context) -> str:
        return owner._handle.name if owner._handle is not None else f"Context@{id(owner):x}"

    def is_readonly(self, name: str) -> bool:
        tool = self._visible(self._tools.get(name, []))
        return tool is not None and tool.readonly

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self.visible_definitions(self._ctx)

    def _available(self, tool: _Tool, ctx: Context) -> bool:
        from .loop import Agent

        return all(
            dependency.optional
            or dependency.key in ctx
            or (dependency.key is ToolCallContext and Agent in ctx)
            for dependency in tool.plan.dependencies.values()
        )

    def visible_definitions(self, ctx: Context) -> tuple[ToolDefinition, ...]:
        return tuple(
            tool.definition
            for entries in self._tools.values()
            if (tool := self._visible(entries)) is not None and self._available(tool, ctx)
        )

    def _visible(self, entries: list[_Tool]) -> _Tool | None:
        return next(
            (tool for tool in reversed(entries) if tool.owner._registrations_active()), None
        )

    def plan(self, call: ToolCall) -> ToolPlan:
        tool = self._visible(self._tools.get(call.name, []))
        if tool is None:
            return ToolPlan(call, ToolScheduling())
        keys = tool.resources(call) if callable(tool.resources) else tool.resources
        if inspect.isawaitable(keys):
            if inspect.iscoroutine(keys):
                keys.close()
            raise TypeError("Resource resolvers must return resource names, not an awaitable")
        return ToolPlan(
            call, ToolScheduling(tool.parallel, tuple(cast(Iterable[str], keys))), binding=tool
        )

    async def execute(
        self,
        call: ToolCall,
        *,
        ctx: Context | None = None,
        call_context: ToolCallContext | None = None,
        _expected: object = _UNSPECIFIED,
        error_formatter: ErrorFormatter | None = None,
    ) -> ToolResult:
        tool = self._visible(self._tools.get(call.name, []))
        if _expected is not _UNSPECIFIED and tool is not _expected:
            return ToolResult.error(
                "Tool registration changed during batch planning; issue a fresh call"
            )
        if tool is None:
            return ToolResult.error(f"Unknown tool: {call.name}")
        ctx = ctx or self._ctx
        if not self._available(tool, ctx):
            return ToolResult.error(f"Tool {call.name} is unavailable: missing injected service")
        try:
            arguments = tool.arguments.parse(call.arguments)
            if call_context is None and any(
                dependency.key is ToolCallContext for dependency in tool.plan.dependencies.values()
            ):
                from .loop import Agent

                call_context = ToolCallContext(call, ctx[Agent])
            extra: dict[type, Any] = (
                {ToolCallContext: call_context} if call_context is not None else {}
            )
            bound = tool.plan.bind(ctx, kwargs=arguments, extra=extra)
            with tool.owner.activate():
                async with asyncio.timeout(tool.timeout):
                    if inspect.iscoroutinefunction(tool.callback):
                        value = await tool.callback(*bound.args, **bound.kwargs)
                    else:
                        value = await run_sync(
                            partial(tool.callback, *bound.args, **bound.kwargs),
                            on_cancel=call_context.signal.set if call_context is not None else None,
                        )
                        if inspect.isawaitable(value):
                            value = await value
            if isinstance(value, ProcResult):
                return ToolResult(
                    result_content(to_dict(value)),
                    "ok" if value.returncode == 0 else "error",
                    exit_code=value.returncode,
                )
            return (
                value if isinstance(value, ToolResult) else ToolResult(tool_result_content(value))
            )
        except asyncio.CancelledError:
            if call_context is not None:
                call_context.signal.set()
            raise
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, error_formatter))
