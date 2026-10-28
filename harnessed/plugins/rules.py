import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from harnessed.core.hooks import ReviewToolResult
from harnessed.core.loop import Agent
from harnessed.core.messages import ToolCall, ToolResult, to_dict, user
from harnessed.core.plugin import Plugin
from harnessed.core.session.events import RuleEvaluated, RuleTriggered, ToolCalled, ToolReturned
from harnessed.core.tokens import TokenCounter
from harnessed.kernel import Inject, hook


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

    def context(self, text: str) -> "Rule":
        """Keep a named context fact while the rule condition remains active."""
        return self._action("context", text)

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
    ):
        if rule.tools and call.name not in rule.tools:
            return None, 0
        arguments = call.command or json.dumps(to_dict(call.arguments), sort_keys=True)
        key = (index, json.dumps([call.name, arguments], ensure_ascii=False))
        if matched is None:
            matched = rule.matches(call, result, counter)
        self._counts[key] = self._counts[key] + 1 if matched else 0
        self._last_counts[index] = self._counts[key]
        return key, self._counts[key]

    async def setup(self, agent: Inject[Agent], counter: Inject[TokenCounter]):
        self._counts: Counter = Counter()
        self._last_counts = {}
        # Legacy per-evaluation records are read only during migration. New logs
        # keep match bits with the canonical result, surviving notes/spill/limits.
        stored = [
            event
            for event in agent.session.events
            if isinstance(event, RuleEvaluated) and event.rule.startswith(f"{self.name}:")
        ]
        for index in range(len(self.rules)):
            for event in stored:
                if event.rule == f"{self.name}:{index}":
                    self._counts[(index, event.key)] = event.occurrence
                    self._last_counts[index] = event.occurrence
        calls = {}
        for event in agent.session.events:
            if isinstance(event, ToolCalled):
                calls[event.call.id] = event.call
            elif isinstance(event, ToolReturned) and event.call_id in calls:
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
                    elif not stored and event.result.status not in ("denied", "unavailable"):
                        self._count(index, rule, calls[event.call_id], event.result, counter)
        for index, rule in enumerate(self.rules):
            for kind, text in rule.actions:
                if kind == "context" and self._last_counts.get(index, 0) >= rule.repeated:
                    await agent.context.set(f"rule:{self.name}:{index}", text)

    @hook(ReviewToolResult)
    async def review(self, event, next_, agent: Inject[Agent], counter: Inject[TokenCounter]):
        result = await next_()
        matches = list(result.rule_matches)
        for index, rule in enumerate(self.rules):
            key, count = self._count(index, rule, event.call, result, counter)
            if key is not None:
                rule_id = f"{self.name}:{index}"
                if any(name == rule_id for name, _ in matches):
                    raise ValueError(f"Duplicate rules plugin name: {self.name!r}")
                matches.append((rule_id, count > 0))
            if count == 0 or (rule.repeated > 1 and count != rule.repeated):
                if (
                    key is not None
                    and count == 0
                    and any(kind == "context" for kind, _ in rule.actions)
                ):
                    await agent.context.remove(f"rule:{self.name}:{index}")
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
                elif kind == "context":
                    await agent.context.set(f"rule:{self.name}:{index}", text)
                else:
                    await agent.steer(user(text))
        return replace(result, rule_matches=tuple(matches))

    async def teardown(self, agent: Inject[Agent]):
        for index, rule in enumerate(self.rules):
            if any(kind == "context" for kind, _ in rule.actions):
                await agent.context.remove(f"rule:{self.name}:{index}")
