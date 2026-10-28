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


class PromptPlan:
    """InstructionValues first, then guidance in plugin order; no implicit headings."""

    def __init__(self, instructions: InstructionValues = (), plugins: Iterable = ()):
        plugins = tuple(plugins)
        self.context_entries = tuple(
            description
            for plugin in plugins
            if (description := getattr(plugin, "context_description", None)) is not None
        )
        values = (instructions,) if isinstance(instructions, str) else tuple(instructions)
        entries: dict[str, PromptEntry] = {}
        literal_values = {value for value in values if isinstance(value, str)}
        literal_values.update(
            value.text
            for value in values
            if isinstance(value, Section) and value._file is None and value._factory is None
        )

        def add(value: str | Section, origin: str):
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
        for index, spec in enumerate(plugins):
            guidance = getattr(spec, "guidance", None)
            if guidance is None:
                continue
            if not isinstance(guidance, str | Section):
                raise TypeError("Plugin guidance must be a string, Section or None")
            literal = (
                guidance
                if isinstance(guidance, str)
                else (
                    guidance.text if guidance._file is None and guidance._factory is None else None
                )
            )
            if literal is not None and literal in literal_values:
                continue
            label = getattr(spec, "name", type(spec).__name__)
            add(guidance, f"{label}.guidance (plugins[{index}])")
        self.entries = tuple(entries.values())

    async def render(self, ctx: Context | None = None) -> str:
        return "\n\n".join(
            [text for entry in self.entries if (text := await entry.section.render(ctx))]
        )

    def __str__(self) -> str:
        lines = [f"System prompt ({len(self.entries)} sections)"]
        for index, entry in enumerate(self.entries, 1):
            preview = (
                entry.section.description or str(entry.section._file)
                if entry.section._file
                else entry.section.description or entry.section.text[:80]
            )
            lines.append(f"  {index}. {entry.section.name}: {preview!r} - {entry.origin}")
        if self.context_entries:
            lines.append("Context (dynamic, outside the system prompt)")
            lines.extend(f"  - {key}: {preview}" for key, preview in self.context_entries)
        return "\n".join(lines)
