from __future__ import annotations

import inspect
import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Annotated

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, TypeAdapter
from pydantic.dataclasses import dataclass

from harnessed.core.hooks import ReviewToolResult
from harnessed.core.messages import ToolCall, ToolResult, to_dict, user
from harnessed.core.plugin import Plugin
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.runtime.inbox import SessionInbox
from harnessed.core.session.events import (
    RuleAction,
    RuleTriggeredEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
)
from harnessed.core.session.session import SessionLog
from harnessed.core.tokens import TokenCounter, count_content
from harnessed.kernel import Inject, Next, PluginState, hook


def _synchronous(callback):
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        type(callback).__call__
    ):
        raise ValueError("Rule conditions must be synchronous")
    return callback


type RuleText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
type ExitCondition = Annotated[Callable[[int], bool], AfterValidator(_synchronous)]
type ResultCondition = Annotated[
    Callable[[ToolCall, ToolResult], bool], AfterValidator(_synchronous)
]
_CONFIG = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
_BOOL = TypeAdapter(bool, config=ConfigDict(strict=True, hide_input_in_errors=True))


def _condition(callback, *args) -> bool:
    result = callback(*args)
    if inspect.iscoroutine(result):
        result.close()
    return _BOOL.validate_python(result)


@dataclass(frozen=True, config=_CONFIG)
class Rule:
    __hash__ = None
    tools: tuple[RuleText, ...] = ()
    command_contains: str | None = None
    exit_code: int | ExitCondition | None = None
    repeated: Annotated[int, Field(ge=1)] = 1
    result_tokens_over: Annotated[int, Field(ge=0)] | None = None
    predicate: ResultCondition | None = None
    actions: tuple[tuple[RuleAction, RuleText], ...] = ()

    def _action(self, kind: RuleAction, text: str) -> Rule:
        return replace(self, actions=(*self.actions, (kind, text)))

    def note(self, text: str) -> Rule:
        return self._action(RuleAction.NOTE, text)

    def context(self, text: str) -> Rule:
        """Keep a named context fact while the rule condition remains active."""
        return self._action(RuleAction.CONTEXT, text)

    def steer(self, text: str) -> Rule:
        return self._action(RuleAction.STEER, text)

    def matches(self, call: ToolCall, result: ToolResult, counter: TokenCounter) -> bool:
        if self.command_contains is not None and self.command_contains not in call.command:
            return False
        if self.exit_code is not None:
            if result.exit_code is None:
                return False
            if callable(self.exit_code):
                if not _condition(self.exit_code, result.exit_code):
                    return False
            elif result.exit_code != self.exit_code:
                return False
        if (
            self.result_tokens_over is not None
            and count_content(counter, result.content) <= self.result_tokens_over
        ):
            return False
        return self.predicate is None or _condition(self.predicate, call, result)


def when(
    *,
    tool: str | Iterable[str] | None = None,
    command_contains: str | None = None,
    exit_code: int | Callable[[int], bool] | None = None,
    repeated: int = 1,
    result_tokens_over: int | None = None,
    predicate: Callable[[ToolCall, ToolResult], bool] | None = None,
) -> Rule:
    tools = () if tool is None else (tool,) if isinstance(tool, str) else tuple(tool)
    return Rule(tools, command_contains, exit_code, repeated, result_tokens_over, predicate)


class Rules(Plugin, needs=(SessionLog, DynamicContext, TokenCounter, SessionInbox)):
    _counts = PluginState[Counter[tuple[int, str]]](Counter)
    _last_counts = PluginState[dict[int, int]](dict)

    def __init__(self, rules: Iterable[Rule], *, name: str = "rules") -> None:
        super().__init__(name=name)
        self.rules = tuple(rules)
        if any(not isinstance(rule, Rule) or not rule.actions for rule in self.rules):
            raise ValueError("Each rule needs at least one note, context or steer action")

    def _count(
        self,
        index: int,
        rule: Rule,
        call: ToolCall,
        result: ToolResult,
        counter: TokenCounter,
        *,
        matched: bool | None = None,
    ) -> tuple[tuple[int, str] | None, int]:
        if rule.tools and call.name not in rule.tools:
            return None, 0
        arguments = call.command or json.dumps(to_dict(call.arguments), sort_keys=True)
        key = (index, json.dumps([call.name, arguments], ensure_ascii=False))
        if matched is None:
            matched = rule.matches(call, result, counter)
        self._counts[key] = self._counts[key] + 1 if matched else 0
        self._last_counts[index] = self._counts[key]
        return key, self._counts[key]

    async def setup(
        self,
        session: Inject[SessionLog],
        context: Inject[DynamicContext],
        counter: Inject[TokenCounter],
    ) -> None:
        # Match bits are recorded with the canonical tool result.
        calls: dict[str, ToolCall] = {}
        for event in session.events:
            if isinstance(event, ToolCallRequestedEvent):
                calls[event.call.id] = event.call
            elif isinstance(event, ToolCallFinishedEvent) and event.call_id in calls:
                recorded = dict(event.result.rule_matches)
                for index, rule in enumerate(self.rules):
                    rule_id = f"{self.name}:{index}"
                    if rule_id in recorded:
                        self._count(
                            index,
                            rule,
                            calls[event.call_id],
                            event.result,
                            counter,
                            matched=recorded[rule_id],
                        )
        for index, rule in enumerate(self.rules):
            for kind, text in rule.actions:
                if kind is RuleAction.CONTEXT and self._last_counts.get(index, 0) >= rule.repeated:
                    await context.set(f"rule:{self.name}:{index}", text)

    @hook(ReviewToolResult)
    async def review(
        self,
        event: ReviewToolResult,
        next_: Next[ToolResult],
        session: Inject[SessionLog],
        context: Inject[DynamicContext],
        inbox: Inject[SessionInbox],
        counter: Inject[TokenCounter],
    ) -> ToolResult:
        result = await next_()
        matches = list(result.rule_matches)
        for index, rule in enumerate(self.rules):
            key, count = self._count(index, rule, event.call, event.result, counter)
            if key is not None:
                rule_id = f"{self.name}:{index}"
                if any(name == rule_id for name, _ in matches):
                    raise ValueError(f"Duplicate rules plugin name: {self.name!r}")
                matches.append((rule_id, count > 0))
            if count == 0 or (rule.repeated > 1 and count != rule.repeated):
                if (
                    key is not None
                    and count == 0
                    and any(kind is RuleAction.CONTEXT for kind, _ in rule.actions)
                ):
                    await context.remove(f"rule:{self.name}:{index}")
                continue
            await session.append(
                RuleTriggeredEvent(
                    f"{self.name}:{index}",
                    event.call.id,
                    count,
                    tuple(kind for kind, _ in rule.actions),
                )
            )
            for kind, text in rule.actions:
                if kind is RuleAction.NOTE:
                    result = result.with_note(text)
                elif kind is RuleAction.CONTEXT:
                    await context.set(f"rule:{self.name}:{index}", text)
                else:
                    await inbox.steer(user(text))
        return replace(result, rule_matches=tuple(matches))

    async def teardown(self, context: Inject[DynamicContext]) -> None:
        for index, rule in enumerate(self.rules):
            if any(kind is RuleAction.CONTEXT for kind, _ in rule.actions):
                await context.remove(f"rule:{self.name}:{index}")
