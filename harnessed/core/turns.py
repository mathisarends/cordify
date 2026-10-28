from collections.abc import Iterable
from dataclasses import dataclass

from harnessed.seams.models import ModelRequest

from .events import ModelRequested, SessionEvent, StepEnd, StepStart, TurnEnd, TurnStart


@dataclass(frozen=True)
class Step:
    turn_id: str
    number: int
    events: tuple[SessionEvent, ...]
    reason: str | None = None

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
    id: str
    events: tuple[SessionEvent, ...]
    steps: tuple[Step, ...]
    reason: str | None = None

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
