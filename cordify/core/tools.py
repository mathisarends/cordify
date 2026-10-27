import asyncio
import inspect
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints, overload

from ..kernel.context import Context
from ..kernel.di import bind_injected, injections
from ..kernel.effects import Disposer, run_sync
from ..seams.models import ToolCall, ToolDefinition, ToolResult, result_content, to_dict
from ..seams.shell import ProcResult
from .execution import ToolCallContext
from .scheduling import ResourceSpec, ToolPlan, ToolScheduling, resource_spec

_UNSPECIFIED = object()


def _schema(annotation: Any) -> dict:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        schema = _schema(args[0])
        descriptions = [item for item in args[1:] if isinstance(item, str)]
        return {**schema, **({"description": descriptions[0]} if descriptions else {})}
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(arg) for arg in args]}
    if origin is Literal:
        return {"enum": list(args)}
    if origin is list:
        return {"type": "array", "items": _schema(args[0])}
    if origin is dict and args[0] is str:
        return {"type": "object", "additionalProperties": _schema(args[1])}
    primitives = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }
    if annotation in primitives:
        return {"type": primitives[annotation]}
    raise TypeError(f"Unsupported tool annotation {annotation!r}; use JSON types or Annotated")


def _validate(value: Any, schema: Mapping, path: str):
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                _validate(value, choice, path)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: value does not match any allowed type")
    if "enum" in schema:
        if not any(type(value) is type(item) and value == item for item in schema["enum"]):
            raise ValueError(f"{path}: expected one of {schema['enum']}")
        return
    kind = schema["type"]
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
        "array": isinstance(value, list | tuple),
        "object": isinstance(value, Mapping),
    }[kind]
    if not valid:
        raise ValueError(f"{path}: expected {kind}")
    if kind == "array":
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "object":
        for key, item in value.items():
            _validate(item, schema["additionalProperties"], f"{path}.{key}")


@dataclass(eq=False)
class _Tool:
    definition: ToolDefinition
    callback: Callable
    owner: Context
    signature: inspect.Signature
    injected: dict[str, type]
    timeout: float | None
    parallel: bool
    resources: ResourceSpec


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
    ):
        def register(callback):
            self.register(
                callback, name=name, timeout=timeout, parallel=parallel, resources=resources
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
    ) -> Disposer:
        owner = Context.current() or self._ctx
        owner._check_open()
        ToolScheduling(parallel)
        resources = resource_spec(resources)
        if timeout is not None and timeout <= 0:
            raise ValueError("Tool timeout must be positive")
        signature = inspect.signature(callback)
        hints = get_type_hints(callback, include_extras=True)
        injected = injections(callback)
        properties, required = {}, []
        for parameter in signature.parameters.values():
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise TypeError(
                    "Tools must have named parameters (no *args, **kwargs or positional-only)"
                )
            if parameter.name in injected:
                continue
            schema = _schema(hints.get(parameter.name, parameter.annotation))
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
            else:
                schema["default"] = parameter.default
            properties[parameter.name] = schema
        definition = ToolDefinition(
            name or callback.__name__,
            inspect.getdoc(callback) or "",
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )
        entry = _Tool(
            definition, callback, owner, signature, injected, timeout, parallel, resources
        )
        entries = self._tools.setdefault(definition.name, [])
        entries.append(entry)

        def remove():
            entries.remove(entry)
            if not entries:
                del self._tools[definition.name]

        return owner.effect(remove)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self.visible_definitions(self._ctx)

    def _available(self, tool: _Tool, ctx: Context) -> bool:
        from .loop import Agent

        return all(
            key in ctx or (key is ToolCallContext and Agent in ctx)
            for key in tool.injected.values()
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
        return ToolPlan(call, ToolScheduling(tool.parallel, keys), binding=tool)

    async def execute(
        self,
        call: ToolCall,
        *,
        ctx: Context | None = None,
        call_context: ToolCallContext | None = None,
        _expected: object = _UNSPECIFIED,
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
            arguments = to_dict(call.arguments)
            for name, value in arguments.items():
                if name not in tool.definition.parameters["properties"]:
                    raise ValueError(f"Unknown model argument: {name}")
                _validate(value, tool.definition.parameters["properties"][name], name)
            if call_context is None and ToolCallContext in tool.injected.values():
                from .loop import Agent

                call_context = ToolCallContext(call, ctx[Agent])
            extra = {ToolCallContext: call_context} if call_context is not None else {}
            bound = bind_injected(tool.callback, ctx, kwargs=arguments, extra=extra)
            tool.signature.bind(*bound.args, **bound.kwargs)
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
            return value if isinstance(value, ToolResult) else ToolResult(result_content(value))
        except asyncio.CancelledError:
            if call_context is not None:
                call_context.signal.set()
            raise
        except Exception as exc:
            return ToolResult.error(f"{type(exc).__name__}: {exc}")
