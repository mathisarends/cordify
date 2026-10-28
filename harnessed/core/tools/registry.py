import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Annotated, Any, cast, overload

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from harnessed.core.messages import ToolCall, ToolResult
from harnessed.core.model import ToolDefinition
from harnessed.core.tools.arguments import ToolArguments, tool_result_content
from harnessed.core.tools.context import ToolContext, ToolContexts
from harnessed.core.tools.scheduling import ResourceSpec, ToolPlan, ToolScheduling, resource_spec
from harnessed.kernel.context import Context
from harnessed.kernel.di import BindingPlan, Dependency, binding_plan
from harnessed.kernel.effects import Disposer, run_sync

log = logging.getLogger(__name__)

_UNSPECIFIED = object()
_TIMEOUT = TypeAdapter(
    Annotated[float, Field(gt=0)] | None,
    config=ConfigDict(
        title="tool timeout", strict=True, allow_inf_nan=False, hide_input_in_errors=True
    ),
)


class ToolNameConflict(ValueError):
    """Two live registrations claim the same name without an explicit override."""


class ToolError(Exception):
    """An intentional, safe error message that may be shown to the model."""


@dataclass(frozen=True)
class ToolSpec:
    """One declaration shared by method, bundle and functional registration."""

    name: str | None = None
    timeout: float | None = None
    parallel: bool = False
    resources: ResourceSpec = ()
    readonly: bool = False
    override: bool = False

    @property
    def needs(self) -> tuple[type, ...]:
        return (ToolRegistry,)

    def mount(self, ctx: Context, callback: Callable):
        return ctx[ToolRegistry].register(callback, spec=self, owner=ctx)


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
    spec: ToolSpec
    arguments: ToolArguments


@dataclass(eq=False)
class _Converter:
    callback: Callable[[Any], ToolResult]
    owner: Context


class ToolRegistry:
    def __init__(self, ctx: Context, *, default_timeout: float | None = None):
        self._ctx = ctx
        self._default_timeout = _TIMEOUT.validate_python(default_timeout)
        self._tools: dict[str, list[_Tool]] = {}
        self._converters: dict[type, list[_Converter]] = {}

    def result_converter[T](
        self, kind: type[T], callback: Callable[[T], ToolResult], *, owner: Context | None = None
    ) -> Disposer:
        """Own a converter for a feature's result value for the plugin lifetime."""
        owner = owner if owner is not None else Context.current() or self._ctx
        owner._check_open()
        entries = self._converters.setdefault(kind, [])
        entry = _Converter(callback, owner)
        entries.append(entry)

        def remove():
            entries.remove(entry)
            if not entries:
                self._converters.pop(kind, None)

        return owner.effect(remove)

    def _result(self, value: Any) -> ToolResult:
        if isinstance(value, ToolResult):
            return value
        for kind in type(value).__mro__:
            for entry in reversed(self._converters.get(kind, ())):
                if entry.owner._registrations_active():
                    return entry.callback(value)
        return ToolResult(tool_result_content(value))

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
        spec: ToolSpec | None = None,
    ) -> Disposer:
        spec = spec or ToolSpec(name, timeout, parallel, resources, readonly, override)
        name, timeout, parallel = spec.name, spec.timeout, spec.parallel
        resources, readonly, override = spec.resources, spec.readonly, spec.override
        owner = owner if owner is not None else Context.current() or self._ctx
        owner._check_open()
        ToolScheduling(parallel)
        resources = resource_spec(resources)
        timeout = _TIMEOUT.validate_python(timeout)
        spec = replace(spec, timeout=timeout)
        plan = binding_plan(callback)
        implicit = {}
        for parameter, annotation in plan.hints.items():
            if annotation is ToolContext and parameter != "return":
                if plan.signature.parameters[parameter].kind is not inspect.Parameter.KEYWORD_ONLY:
                    raise TypeError("Unmarked ToolContext parameters must be keyword-only")
                implicit[parameter] = Dependency(ToolContext)
        if implicit:
            plan = replace(plan, dependencies=MappingProxyType({**plan.dependencies, **implicit}))
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
            spec,
            arguments,
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
        return tool is not None and tool.spec.readonly

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self.visible_definitions(self._ctx)

    def _available(self, tool: _Tool, ctx: Context) -> bool:
        return all(
            dependency.optional
            or dependency.key in ctx
            or (dependency.key is ToolContext and ToolContexts in ctx)
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
        if call.malformed is not None:
            return ToolPlan(
                call,
                ToolScheduling(),
                ToolResult.error("Tool arguments must be a valid JSON object"),
            )
        tool = self._visible(self._tools.get(call.name, []))
        if tool is None:
            return ToolPlan(call, ToolScheduling())
        resources = tool.spec.resources
        keys = resources(call) if callable(resources) else resources
        if inspect.isawaitable(keys):
            if inspect.iscoroutine(keys):
                keys.close()
            raise TypeError("Resource resolvers must return resource names, not an awaitable")
        return ToolPlan(
            call, ToolScheduling(tool.spec.parallel, tuple(cast(Iterable[str], keys))), binding=tool
        )

    async def execute(
        self,
        call: ToolCall,
        *,
        ctx: Context | None = None,
        call_context: ToolContext | None = None,
        _expected: object = _UNSPECIFIED,
        error_formatter: ErrorFormatter | None = None,
    ) -> ToolResult:
        if call.malformed is not None:
            return ToolResult.error("Tool arguments must be a valid JSON object")
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
                dependency.key is ToolContext for dependency in tool.plan.dependencies.values()
            ):
                call_context = ctx[ToolContexts].create(call)
            extra: dict[type, Any] = {ToolContext: call_context} if call_context is not None else {}
            bound = tool.plan.bind(ctx, kwargs=arguments, extra=extra)
            with tool.owner.activate():
                async with asyncio.timeout(
                    tool.spec.timeout if tool.spec.timeout is not None else self._default_timeout
                ):
                    if inspect.iscoroutinefunction(tool.callback):
                        value = await tool.callback(*bound.args, **bound.kwargs)
                    else:
                        value = await run_sync(
                            partial(tool.callback, *bound.args, **bound.kwargs),
                            on_cancel=call_context.signal.set if call_context is not None else None,
                        )
                        if inspect.isawaitable(value):
                            value = await value
            return self._result(value)
        except asyncio.CancelledError:
            if call_context is not None:
                call_context.signal.set()
            raise
        except Exception as exc:
            return ToolResult.error(format_tool_error(call, exc, error_formatter))
