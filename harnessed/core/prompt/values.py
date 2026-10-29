from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Protocol, Self, overload
from xml.sax.saxutils import escape, quoteattr

from harnessed.core.errors import ConfigError
from harnessed.kernel import Context


class PromptFormat(StrEnum):
    PLAIN = "plain"
    XML = "xml"
    MARKDOWN = "markdown"


class Unplaced(StrEnum):
    """Append remaining plugin sections, or require complete placement.

    ERROR checks coverage, not prompt stability: plugin groups and the rest slot
    also place future declarations. Use individual references to detect additions.
    """

    APPEND = "append"
    ERROR = "error"


class SectionDelivery(StrEnum):
    """How runtime mounts handle declared system sections."""

    ERROR = "error"
    CONTEXT = "context"
    IGNORE = "ignore"


class PromptOwner(Protocol):
    name: str


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class SystemSection:
    """System instructions supplied as text, a file, or a session-scoped factory.

    Access on a plugin recipe binds its identity and diagnostic name to that
    recipe without evaluating content.
    """

    text: str = ""
    title: str | None = None
    description: str = ""
    _file: Path | None = field(default=None, init=False, repr=False)
    _missing_ok: bool = field(default=False, init=False, repr=False)
    _factory: Callable[[Context], Awaitable[str | None]] | None = field(
        default=None, init=False, repr=False
    )
    _attribute: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.description, str):
            raise TypeError("System section text and description must be strings")
        if self.title is not None and (
            not isinstance(self.title, str)
            or not self.title.strip()
            or len(self.title.splitlines()) != 1
        ):
            raise ValueError("System section titles must be non-empty single-line strings")

    def __set_name__(self, owner: type, name: str) -> None:
        object.__setattr__(self, "_attribute", name)

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, instance: PromptOwner, owner: type | None = None) -> BoundSystemSection: ...

    def __get__(
        self, instance: PromptOwner | None, owner: type | None = None
    ) -> Self | BoundSystemSection:
        return self if instance is None else self.bind(instance, self._attribute)

    def bind(self, owner: PromptOwner, attribute: str) -> BoundSystemSection:
        return BoundSystemSection(owner=owner, declaration=self, attribute=attribute, section=self)

    @classmethod
    def from_file(
        cls, *, path: str | Path, title: str | None = None, missing_ok: bool = False
    ) -> Self:
        section = cls(title=title)
        object.__setattr__(section, "_file", Path(path))
        object.__setattr__(section, "_missing_ok", missing_ok)
        return section

    @classmethod
    def from_factory(
        cls,
        *,
        factory: Callable[[Context], Awaitable[str | None]],
        title: str | None = None,
        description: str = "",
    ) -> Self:
        """Evaluate once per session, with session-owned registrations and cleanup."""
        if not callable(factory):
            raise TypeError("System section factories must be callable")
        section = cls(title=title, description=description)
        object.__setattr__(section, "_factory", factory)
        return section

    @property
    def literal_text(self) -> str | None:
        return self.text if self._file is None and self._factory is None else None

    @property
    def preview(self) -> str:
        return self.description or (str(self._file) if self._file else self.text[:80])

    async def render(self, ctx: Context | None = None) -> str:
        text: str | None = self.text
        if self._factory is not None:
            if ctx is None:
                raise ValueError("This system section requires a session context")
            text = await self._factory(ctx)
        elif self._file is not None:
            try:
                text = await asyncio.to_thread(self._file.read_text, encoding="utf-8")
            except FileNotFoundError:
                if not self._missing_ok:
                    raise
                text = ""
        if text is not None and not isinstance(text, str):
            raise TypeError("System section factories must return a string or None")
        return text or ""


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class BoundSystemSection:
    """A section reference bound to one configured plugin recipe."""

    owner: PromptOwner
    declaration: object
    attribute: str
    section: SystemSection
    provider: Callable[[], str | None] | None = field(default=None, repr=False)

    @property
    def identity(self) -> tuple[int, int]:
        return id(self.owner), id(self.declaration)

    @property
    def origin(self) -> str:
        return f"{self.owner.name}.{self.attribute}"

    def resolve(self) -> SystemSection:
        if self.provider is None:
            return self.section
        text = self.provider()
        if text is not None and not isinstance(text, str):
            raise TypeError(f"System section {self.origin} must return a string or None")
        return replace(self.section, text=text or "")


class SystemSectionProvider[T: PromptOwner]:
    def __init__(self, callback: Callable[[T], str | None], *, title: str | None) -> None:
        self.callback = callback
        self.section = SystemSection(title=title)
        self.attribute = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.attribute = name

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, instance: T, owner: type | None = None) -> BoundSystemSection: ...

    def __get__(self, instance: T | None, owner: type | None = None) -> Self | BoundSystemSection:
        if instance is None:
            return self
        return BoundSystemSection(
            owner=instance,
            declaration=self,
            attribute=self.attribute,
            section=self.section,
            provider=partial(self.callback, instance),
        )


def system_section[T: PromptOwner](
    *, title: str | None = None
) -> Callable[[Callable[[T], str | None]], SystemSectionProvider[T]]:
    """Declare pure, synchronous system instructions derived from recipe configuration."""

    def decorate(callback: Callable[[T], str | None]) -> SystemSectionProvider[T]:
        if inspect.iscoroutinefunction(callback) or inspect.isasyncgenfunction(callback):
            raise TypeError("@system_section requires a synchronous configuration provider")
        return SystemSectionProvider(callback, title=title)

    return decorate


@dataclass(frozen=True, slots=True)
class PluginSections:
    """Place one plugin's sections, or all remaining sections when plugin is None.

    A complete prompt may contain at most one remaining-sections slot, including
    nested documents. Explicit placements and exclusions are resolved first.
    """

    plugin: PromptOwner | None = None


type PromptPart = str | SystemSection | BoundSystemSection | PluginSections | SystemPrompt


@dataclass(frozen=True, slots=True, init=False)
class SystemPrompt:
    """An ordered document with automatic placement of remaining plugin sections.

    Nested documents inherit the outermost format and unplaced policy.
    """

    sections: tuple[PromptPart, ...]
    format: PromptFormat
    exclude: tuple[BoundSystemSection | SystemSection, ...]
    unplaced: Unplaced

    def __init__(
        self,
        *,
        sections: Iterable[PromptPart] = (),
        format: PromptFormat = PromptFormat.PLAIN,
        exclude: Iterable[BoundSystemSection | SystemSection] = (),
        unplaced: Unplaced = Unplaced.APPEND,
    ) -> None:
        object.__setattr__(self, "sections", tuple(sections))
        object.__setattr__(self, "format", PromptFormat(format))
        object.__setattr__(self, "exclude", tuple(exclude))
        object.__setattr__(self, "unplaced", Unplaced(unplaced))


@dataclass(frozen=True, slots=True)
class PromptContribution:
    name: str
    sections: tuple[BoundSystemSection, ...] = ()
    context_description: tuple[str, str] | None = None
    owner: PromptOwner | None = None


@dataclass(frozen=True, slots=True)
class PromptEntry:
    section: SystemSection
    origin: str


class PromptComposition:
    """Validate placement before evaluating any configuration-derived sections."""

    def __init__(
        self,
        prompt: str | SystemPrompt | None = None,
        contributions: Iterable[PromptContribution] = (),
    ) -> None:
        contributions = tuple(contributions)
        if isinstance(prompt, str):
            prompt = SystemPrompt(sections=(prompt,))
        if prompt is None:
            prompt = SystemPrompt()
        if not isinstance(prompt, SystemPrompt):
            raise TypeError("system_prompt must be a string or SystemPrompt")
        self.format = prompt.format
        self.context_entries = tuple(
            description
            for contribution in contributions
            if (description := contribution.context_description) is not None
        )
        available = {
            section.identity: section
            for contribution in contributions
            for section in contribution.sections
        }
        by_declaration: dict[int, list[BoundSystemSection]] = {}
        for section in available.values():
            by_declaration.setdefault(id(section.declaration), []).append(section)
        selected: list[SystemSection | BoundSystemSection | PluginSections] = []
        excluded: list[SystemSection | BoundSystemSection] = []

        def flatten(group: SystemPrompt) -> None:
            excluded.extend(group.exclude)
            for part in group.sections:
                if isinstance(part, SystemPrompt):
                    flatten(part)
                elif isinstance(part, str):
                    selected.append(SystemSection(text=part))
                elif isinstance(part, SystemSection | BoundSystemSection | PluginSections):
                    selected.append(part)
                else:
                    raise TypeError(
                        "SystemPrompt sections must be strings, section references or groups"
                    )

        flatten(prompt)
        seen: set[tuple[int, int]] = set()
        literal_seen: set[int] = set()

        def reference(
            value: SystemSection | BoundSystemSection,
        ) -> SystemSection | BoundSystemSection:
            if isinstance(value, BoundSystemSection):
                if value.identity not in available:
                    raise ConfigError(
                        f"System section {value.origin} belongs to an uninstalled plugin"
                    )
                return available[value.identity]
            if not isinstance(value, SystemSection):
                raise TypeError("Exclusions must be system section references")
            matches = by_declaration.get(id(value), [])
            if len(matches) > 1:
                raise ConfigError("Ambiguous section; reference it through a configured plugin")
            return matches[0] if matches else value

        def claim(value: SystemSection | BoundSystemSection, *, exclusion: bool) -> None:
            if isinstance(value, BoundSystemSection):
                if value.identity in seen:
                    raise ConfigError(f"System section {value.origin} was placed or excluded twice")
                seen.add(value.identity)
            else:
                if exclusion:
                    raise ConfigError("Excluded section does not belong to an installed plugin")
                if id(value) in literal_seen:
                    raise ConfigError("The same system section was placed twice")
                literal_seen.add(id(value))

        excluded = [reference(value) for value in excluded]
        for value in excluded:
            claim(value, exclusion=True)
        excluded_ids = set(seen)
        placed: list[SystemSection | BoundSystemSection | PluginSections] = []
        rest: PluginSections | None = None
        for value in selected:
            if isinstance(value, PluginSections):
                if value.plugin is None:
                    if rest is not None:
                        raise ConfigError(
                            "Only one PluginSections() rest slot is allowed per prompt"
                        )
                    rest = value
                    placed.append(value)
                    continue
                contribution = next(
                    (item for item in contributions if item.owner is value.plugin), None
                )
                if contribution is None:
                    raise ConfigError("PluginSections refers to an uninstalled plugin")
                for section in contribution.sections:
                    if section.identity not in excluded_ids:
                        claim(section, exclusion=False)
                        placed.append(section)
            else:
                section = reference(value)
                claim(section, exclusion=False)
                placed.append(section)
        remaining = [section for identity, section in available.items() if identity not in seen]
        resolved: list[SystemSection | BoundSystemSection] = []
        for value in placed:
            if isinstance(value, PluginSections):
                resolved.extend(remaining)
            else:
                resolved.append(value)
        if rest is None:
            if remaining and prompt.unplaced is Unplaced.ERROR:
                raise ConfigError(
                    "Unplaced system sections: "
                    + ", ".join(section.origin for section in remaining)
                )
            resolved.extend(remaining)
        self.excluded = tuple(
            value.origin for value in excluded if isinstance(value, BoundSystemSection)
        )
        self.entries = tuple(
            PromptEntry(value.resolve(), value.origin)
            if isinstance(value, BoundSystemSection)
            else PromptEntry(value, value.title or "application section")
            for value in resolved
        )

    async def render(self, ctx: Context | None = None) -> str:
        parts: list[str] = []
        for entry in self.entries:
            text = await entry.section.render(ctx)
            if not text:
                continue
            title = entry.section.title
            match self.format:
                case PromptFormat.PLAIN:
                    parts.append(text)
                case PromptFormat.MARKDOWN:
                    parts.append(f"## {title}\n\n{text}" if title else text)
                case PromptFormat.XML:
                    attribute = f" title={quoteattr(title)}" if title else ""
                    parts.append(f"<section{attribute}>\n{escape(text)}\n</section>")
        return "\n\n".join(parts)

    def __str__(self) -> str:
        lines = [f"System prompt ({self.format.value}, {len(self.entries)} sections)"]
        for index, entry in enumerate(self.entries, 1):
            lines.append(f"  {index}. {entry.origin}: {entry.section.preview!r}")
        lines.extend(f"  excluded: {origin}" for origin in self.excluded)
        if self.context_entries:
            lines.append("Context (dynamic, outside the system prompt)")
            lines.extend(f"  - {key}: {preview}" for key, preview in self.context_entries)
        return "\n".join(lines)
