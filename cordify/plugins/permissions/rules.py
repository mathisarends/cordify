import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from cordify.core.events import (
    ReviewToolResult,
    RuleEvaluated,
    RuleTriggered,
    ToolCalled,
    ToolReturned,
)
from cordify.core.features import Plugin
from cordify.core.loop import Agent
from cordify.kernel import Inject, hook
from cordify.seams.models import ToolCall, ToolResult, system, to_dict, user
from cordify.seams.tokens import TokenCounter


@dataclass(frozen=True)
class Rule:
    tools: tuple[str, ...] = ()
    command_contains: str | None = None
    exit_code: int | Callable[[int], bool] | None = None
    repeated: int = 1
    result_tokens_over: int | None = None
    predicate: Callable[[ToolCall, ToolResult], bool] | None = None
    actions: tuple[tuple[str, str], ...] = ()

    def _action(self, kind: str, text: str) -> "Rule":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Rule actions require non-empty text")
        return replace(self, actions=(*self.actions, (kind, text)))

    def note(self, text: str) -> "Rule":
        return self._action("note", text)

    def inject(self, text: str) -> "Rule":
        return self._action("inject", text)

    def steer(self, text: str) -> "Rule":
        return self._action("steer", text)

    def matches(self, call: ToolCall, result: ToolResult, counter: TokenCounter) -> bool:
        if self.command_contains is not None and self.command_contains not in call.command:
            return False
        if self.exit_code is not None:
            if result.exit_code is None:
                return False
            if callable(self.exit_code):
                if not self.exit_code(result.exit_code):
                    return False
            elif result.exit_code != self.exit_code:
                return False
        if (
            self.result_tokens_over is not None
            and counter.count_text(result.content) <= self.result_tokens_over
        ):
            return False
        return self.predicate is None or self.predicate(call, result)


def when(
    *,
    tool: str | Iterable[str] | None = None,
    command_contains: str | None = None,
    exit_code: int | Callable[[int], bool] | None = None,
    repeated: int = 1,
    result_tokens_over: int | None = None,
    predicate: Callable[[ToolCall, ToolResult], bool] | None = None,
) -> Rule:
    if type(repeated) is not int or repeated < 1:
        raise ValueError("repeated must be a positive integer")
    if result_tokens_over is not None and (
        type(result_tokens_over) is not int or result_tokens_over < 0
    ):
        raise ValueError("result_tokens_over must be a non-negative integer")
    tools = () if tool is None else (tool,) if isinstance(tool, str) else tuple(tool)
    if any(not isinstance(name, str) or not name for name in tools):
        raise ValueError("Tool names must be non-empty strings")
    return Rule(tools, command_contains, exit_code, repeated, result_tokens_over, predicate)


class Rules(Plugin):
    def __init__(self, rules: Iterable[Rule], *, name: str = "rules"):
        super().__init__(name=name)
        self.rules = tuple(rules)
        if any(not isinstance(rule, Rule) or not rule.actions for rule in self.rules):
            raise ValueError("Each rule needs at least one note, inject or steer action")
        self._counts: Counter = Counter()

    def _count(
        self, index: int, rule: Rule, call: ToolCall, result: ToolResult, counter: TokenCounter
    ):
        if rule.tools and call.name not in rule.tools:
            return None, 0
        arguments = call.command or json.dumps(to_dict(call.arguments), sort_keys=True)
        key = (index, json.dumps([call.name, arguments], ensure_ascii=False))
        self._counts[key] = self._counts[key] + 1 if rule.matches(call, result, counter) else 0
        return key, self._counts[key]

    async def setup(self, agent: Inject[Agent], counter: Inject[TokenCounter]):
        self._counts.clear()
        stored = [event for event in agent.session.events if isinstance(event, RuleEvaluated)]
        restored = False
        for index in range(len(self.rules)):
            for event in stored:
                if event.rule == f"{self.name}:{index}":
                    self._counts[(index, event.key)] = event.occurrence
                    restored = True
        if restored:
            return
        calls = {}
        for event in agent.session.events:
            if isinstance(event, ToolCalled):
                calls[event.call.id] = event.call
            elif (
                isinstance(event, ToolReturned)
                and event.call_id in calls
                and event.result.status != "denied"
            ):
                for index, rule in enumerate(self.rules):
                    self._count(index, rule, calls[event.call_id], event.result, counter)

    @hook(ReviewToolResult)
    async def review(self, event, next_, agent: Inject[Agent], counter: Inject[TokenCounter]):
        result = await next_()
        for index, rule in enumerate(self.rules):
            key, count = self._count(index, rule, event.call, result, counter)
            if key is not None:
                await agent.session.append(RuleEvaluated(f"{self.name}:{index}", key[1], count))
            if count == 0 or (rule.repeated > 1 and count != rule.repeated):
                continue
            await agent.session.append(
                RuleTriggered(
                    f"{self.name}:{index}",
                    event.call.id,
                    count,
                    tuple(kind for kind, _ in rule.actions),
                )
            )
            for kind, text in rule.actions:
                if kind == "note":
                    result = result.with_note(text)
                elif kind == "inject":
                    await agent.inject(system(text))
                else:
                    await agent.steer(user(text))
        return result
