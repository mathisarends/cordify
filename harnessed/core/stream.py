"""Live events that are deliberately separate from the durable session log."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harnessed.kernel.events import Emit

if TYPE_CHECKING:
    from harnessed.core.session.session import Session


class TransientEvent(Emit):
    """A live notification; never accepted by Session.append or the log codec."""


@dataclass(frozen=True, slots=True)
class TextDelta(TransientEvent):
    message_id: str
    text: str = ""
    reasoning: str = ""


@dataclass(frozen=True, slots=True)
class SessionPublished(Emit):
    session: "Session"
    event: TransientEvent
