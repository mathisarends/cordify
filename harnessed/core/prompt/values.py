import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from harnessed.kernel import Context


@dataclass(frozen=True, slots=True)
class Section:
    """A named literal section, or a file read once when a session is opened."""

    name: str
    text: str = ""
    override: bool = False
    _file: Path | None = field(default=None, repr=False)
    _missing_ok: bool = field(default=False, repr=False)
    _factory: Callable[[Context], Awaitable[str]] | None = field(default=None, repr=False)
    description: str = ""

    @classmethod
    def from_file(cls, path: str | Path, *, missing_ok: bool = False) -> Self:
        path = Path(path)
        return cls(str(path), _file=path, _missing_ok=missing_ok)

    @classmethod
    def from_factory(
        cls, name: str, factory: Callable[[Context], Awaitable[str]], *, description: str = ""
    ) -> Self:
        """Render session-scoped guidance once through its ordinary plugin context."""
        return cls(name, _factory=factory, description=description)

    @property
    def literal_text(self) -> str | None:
        return self.text if self._file is None and self._factory is None else None

    @property
    def preview(self) -> str:
        return self.description or (str(self._file) if self._file else self.text[:80])

    async def render(self, ctx: Context | None = None) -> str:
        if self._factory is not None:
            if ctx is None:
                raise ValueError(f"Section {self.name!r} requires a session context")
            return await self._factory(ctx)
        if self._file is None:
            return self.text
        try:
            return await asyncio.to_thread(self._file.read_text, encoding="utf-8")
        except FileNotFoundError:
            if self._missing_ok:
                return ""
            raise


type InstructionValues = str | Iterable[str | Section]


@dataclass(frozen=True, slots=True)
class PromptEntry:
    section: Section
    origin: str


@dataclass(frozen=True, slots=True)
class PromptContribution:
    name: str
    guidance: str | Section | None = None
    context_description: tuple[str, str] | None = None


class PromptComposition:
    """InstructionValues first, then guidance in plugin order; no implicit headings."""

    def __init__(
        self, instructions: InstructionValues = (), contributions: Iterable[PromptContribution] = ()
    ) -> None:
        contributions = tuple(contributions)
        self.context_entries = tuple(
            description
            for contribution in contributions
            if (description := contribution.context_description) is not None
        )
        values = (instructions,) if isinstance(instructions, str) else tuple(instructions)
        entries: dict[str, PromptEntry] = {}
        literal_values = {value for value in values if isinstance(value, str)}
        literal_values.update(
            value.literal_text
            for value in values
            if isinstance(value, Section) and value.literal_text is not None
        )

        def add(value: str | Section, origin: str) -> None:
            section = value if isinstance(value, Section) else Section(origin, value)
            if section.name in entries and not section.override:
                raise ValueError(
                    f"Duplicate instruction section {section.name!r}; use override=True"
                )
            entries.pop(section.name, None)
            entries[section.name] = PromptEntry(section, origin)

        for index, value in enumerate(values):
            if not isinstance(value, str | Section):
                raise TypeError("InstructionValues must contain strings or Section values")
            add(value, f"instructions[{index}]")
        for index, contribution in enumerate(contributions):
            guidance = contribution.guidance
            if guidance is None:
                continue
            literal = guidance if isinstance(guidance, str) else guidance.literal_text
            if literal is not None and literal in literal_values:
                continue
            label = contribution.name
            add(guidance, f"{label}.guidance (plugins[{index}])")
        self.entries = tuple(entries.values())

    async def render(self, ctx: Context | None = None) -> str:
        return "\n\n".join(
            [text for entry in self.entries if (text := await entry.section.render(ctx))]
        )

    def __str__(self) -> str:
        lines = [f"System prompt ({len(self.entries)} sections)"]
        for index, entry in enumerate(self.entries, 1):
            preview = entry.section.preview
            lines.append(f"  {index}. {entry.section.name}: {preview!r} - {entry.origin}")
        if self.context_entries:
            lines.append("Context (dynamic, outside the system prompt)")
            lines.extend(f"  - {key}: {preview}" for key, preview in self.context_entries)
        return "\n".join(lines)
