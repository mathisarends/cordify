from collections.abc import Iterable
from dataclasses import dataclass

from harnessed.core.model import ModelRequest
from harnessed.core.session.events import (
    ModelRequested,
    SessionEvent,
    StepEnd,
    StepOutcome,
    StepStart,
    TurnEnd,
    TurnOutcome,
    TurnStart,
)


@dataclass(frozen=True)
class Step:
    __hash__ = None
    turn_id: str
    number: int
    events: tuple[SessionEvent, ...]
    reason: StepOutcome | None = None

    def __post_init__(self):
        object.__setattr__(self, "events", tuple(self.events))
        count = sum(isinstance(event, ModelRequested) for event in self.events)
        if count > 1 or (self.reason == "completed" and count != 1):
            raise ValueError("A completed step must contain exactly one model request")

    @property
    def request(self) -> ModelRequest | None:
        return next(
            (event.request for event in self.events if isinstance(event, ModelRequested)), None
        )

    @property
    def closed(self) -> bool:
        return self.reason is not None


@dataclass(frozen=True)
class Turn:
    __hash__ = None
    id: str
    events: tuple[SessionEvent, ...]
    steps: tuple[Step, ...]
    reason: TurnOutcome | None = None

    def __post_init__(self):
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def closed(self) -> bool:
        return self.reason is not None


def derive_turns(events: Iterable[SessionEvent]) -> tuple[Turn, ...]:
    events = tuple(events)
    turns, steps = [], []
    turn_start = step_start = None
    turn_id = None
    step_number = 0
    for seq, event in enumerate(events):
        if isinstance(event, TurnStart):
            if turn_start is not None:
                raise ValueError("A turn cannot start inside an open turn")
            turn_start, turn_id, steps = seq, event.turn_id, []
        elif isinstance(event, StepStart):
            if turn_start is None or event.turn_id != turn_id or step_start is not None:
                raise ValueError("A step must start inside its turn, after the previous step")
            if type(event.step) is not int or event.step != len(steps) + 1:
                raise ValueError("Steps must be numbered consecutively from one within each turn")
            step_start, step_number = seq, event.step
        elif isinstance(event, StepEnd):
            if step_start is None or (event.turn_id, event.step) != (turn_id, step_number):
                raise ValueError("StepEnd must close the current step")
            assert turn_id is not None
            steps.append(Step(turn_id, step_number, events[step_start : seq + 1], event.reason))
            step_start = None
        elif isinstance(event, TurnEnd):
            if turn_start is None or event.turn_id != turn_id or step_start is not None:
                raise ValueError("TurnEnd must close the current turn after its last step")
            assert turn_id is not None
            turns.append(Turn(turn_id, events[turn_start : seq + 1], tuple(steps), event.reason))
            turn_start, turn_id = None, None
    if turn_start is not None:
        assert turn_id is not None
        if step_start is not None:
            steps.append(Step(turn_id, step_number, events[step_start:]))
        turns.append(Turn(turn_id, events[turn_start:], tuple(steps)))
    return tuple(turns)


class TurnProjection:
    """Incremental builder; immutable snapshots never reference its mutable buffers."""

    def __init__(self, events: Iterable[SessionEvent] = ()):
        self._closed: list[Turn] = []
        self._turn: str | None = None
        self._events: list[SessionEvent] = []
        self._steps: list[Step] = []
        self._step: list[SessionEvent] | None = None
        self._requests = 0
        self._open_snapshot: Turn | None = None
        self._snapshot: tuple[Turn, ...] | None = None
        for event in events:
            self.validate(event)
            self.apply(event)

    @property
    def closed_count(self) -> int:
        return len(self._closed)

    def validate(self, event: SessionEvent) -> None:
        if isinstance(event, TurnStart):
            if self._turn is not None:
                raise ValueError("A turn cannot start inside an open turn")
        elif isinstance(event, StepStart):
            if self._turn is None or event.turn_id != self._turn or self._step is not None:
                raise ValueError("A step must start inside its turn, after the previous step")
            if type(event.step) is not int or event.step != len(self._steps) + 1:
                raise ValueError("Steps must be numbered consecutively from one within each turn")
        elif isinstance(event, StepEnd):
            if self._step is None or (event.turn_id, event.step) != (
                self._turn,
                len(self._steps) + 1,
            ):
                raise ValueError("StepEnd must close the current step")
            if event.reason == "completed" and self._requests != 1:
                raise ValueError("A completed step must contain exactly one model request")
        elif isinstance(event, TurnEnd):
            if self._turn is None or event.turn_id != self._turn or self._step is not None:
                raise ValueError("TurnEnd must close the current turn after its last step")
        elif isinstance(event, ModelRequested) and self._step is not None and self._requests:
            raise ValueError("A completed step must contain exactly one model request")

    def apply(self, event: SessionEvent) -> None:
        if isinstance(event, TurnStart):
            self._turn, self._events, self._steps = event.turn_id, [], []
        if self._turn is None:
            return
        self._snapshot = self._open_snapshot = None
        self._events.append(event)
        if isinstance(event, StepStart):
            self._step, self._requests = [], 0
        if self._step is not None:
            self._step.append(event)
            if isinstance(event, ModelRequested):
                self._requests += 1
        if isinstance(event, StepEnd):
            assert self._step is not None
            self._steps.append(Step(self._turn, event.step, tuple(self._step), event.reason))
            self._step = None
        if isinstance(event, TurnEnd):
            self._closed.append(
                Turn(self._turn, tuple(self._events), tuple(self._steps), event.reason)
            )
            self._turn, self._events, self._steps = None, [], []

    @property
    def open_turn(self) -> Turn | None:
        if self._turn is None:
            return None
        if self._open_snapshot is None:
            steps = tuple(self._steps)
            if self._step is not None:
                steps = (*steps, Step(self._turn, len(steps) + 1, tuple(self._step)))
            self._open_snapshot = Turn(self._turn, tuple(self._events), steps)
        return self._open_snapshot

    @property
    def snapshot(self) -> tuple[Turn, ...]:
        if self._snapshot is None:
            current = self.open_turn
            self._snapshot = (
                (*self._closed, current) if current is not None else tuple(self._closed)
            )
        return self._snapshot
