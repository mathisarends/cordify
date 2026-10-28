import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Protocol
from xml.sax.saxutils import escape, quoteattr

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from harnessed.core.errors import ConfigError
from harnessed.core.hooks import SessionAppended
from harnessed.core.messages import freeze, to_dict
from harnessed.core.plugin import Plugin
from harnessed.core.prompt.context import DynamicContext
from harnessed.core.prompt.values import Section
from harnessed.core.session.events import CompactionApplied, ToolCalled, ToolCompleted, ToolReturned
from harnessed.core.session.session import SessionLog
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.registry import ToolError
from harnessed.kernel import Context, Inject, Key, hook

type SkillName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
]
type Description = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class SkillInfo(BaseModel):
    """Frozen catalog metadata; instructions are disclosed separately on demand."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)
    __hash__ = None
    name: SkillName
    description: Description
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _immutable(self):
        if not self.description.strip():
            raise ValueError("Skill descriptions cannot be blank")
        try:
            object.__setattr__(self, "metadata", freeze(self.metadata))
        except TypeError as error:
            raise ValueError("Skill metadata must contain finite JSON values") from error
        return self

    @field_serializer("metadata")
    def _serialize_metadata(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return to_dict(value)


@dataclass(frozen=True, slots=True)
class _DirectorySkill:
    path: Path


class Skill(SkillInfo):
    """An inline skill, or a deferred directory source via from_dir()."""

    __hash__ = None
    instructions: str

    @classmethod
    def from_dir(cls, path: str | Path) -> _DirectorySkill:
        """Describe a single skill folder without reading it before session startup."""
        return _DirectorySkill(Path(path))


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One selected skill and the later sources it shadows."""

    __hash__ = None
    info: SkillInfo
    directory: Path | None = None
    inline: Skill | None = None
    shadows: tuple[str, ...] = ()


class SkillCatalog(Protocol):
    """The immutable, inspectable catalog belonging to the current session."""

    @property
    def entries(self) -> tuple[SkillEntry, ...]: ...


@dataclass(frozen=True, slots=True)
class _Catalog:
    entries: tuple[SkillEntry, ...]


def _yaml():
    try:
        import yaml
    except ImportError as error:
        raise ConfigError("Install skill folder support: uv add 'harnessed[skills]'") from error
    return yaml


def _metadata(text: str) -> dict:
    yaml = _yaml()

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            result = {}
            for key, value in node.value:
                name = self.construct_object(key, deep=deep)
                if name in result:
                    raise ValueError(f"Duplicate frontmatter key: {name}")
                result[name] = self.construct_object(value, deep=deep)
            return result

    value = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("Skill frontmatter must be a mapping")
    return value


def _inside(root: Path, relative: str) -> Path:
    if (
        not relative
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or PureWindowsPath(relative).root
        or ".." in relative.replace("\\", "/").split("/")
    ):
        raise ValueError("Skill file paths must stay inside their skill directory")
    if root.resolve() != root:
        raise ValueError("The skill directory changed its resolved location")
    path = (root / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Skill file paths must name regular files inside their skill directory")
    return path


def _read(root: Path, *, body: bool = False) -> tuple[SkillInfo, str | None]:
    path = root / "SKILL.md"
    try:
        path = _inside(root, "SKILL.md")
        with path.open(encoding="utf-8", newline="") as stream:
            if stream.readline().lstrip("\ufeff").strip() != "---":
                raise ValueError("SKILL.md must begin with YAML frontmatter")
            header = []
            for line in stream:
                if line.rstrip("\r\n \t") == "---":
                    break
                header.append(line)
            else:
                raise ValueError("Skill frontmatter has no closing --- delimiter")
            data = _metadata("".join(header))
            name, description = data.pop("name", None), data.pop("description", None)
            compatibility = data.get("compatibility")
            if "compatibility" in data and (
                not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500
            ):
                raise ValueError("compatibility must contain 1-500 characters")
            metadata = data.get("metadata")
            if "metadata" in data and (
                not isinstance(metadata, dict)
                or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()
                )
            ):
                raise ValueError("metadata must map string keys to string values")
            for field in ("license", "allowed-tools"):
                if field in data and not isinstance(data[field], str):
                    raise ValueError(f"{field} must be a string")
            info = SkillInfo(name=name, description=description, metadata=data)
            if info.name != root.name:
                raise ValueError("Skill name must match its directory name")
            return info, stream.read() if body else None
    except Exception as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError(f"Invalid skill at {path}: {error}") from error


def _directory(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Source is not a directory")
        return root
    except (OSError, ValueError, RuntimeError) as error:
        raise ConfigError(f"Invalid skill source {path}: {error}") from error


def _discover(sources) -> _Catalog:
    entries: dict[str, SkillEntry] = {}

    def add(entry: SkillEntry):
        name = entry.info.name
        if name in entries:
            prior = entries[name]
            entries[name] = SkillEntry(
                prior.info,
                prior.directory,
                prior.inline,
                (*prior.shadows, str(entry.directory) if entry.directory is not None else "inline"),
            )
        else:
            entries[name] = entry

    for source in sources:
        if isinstance(source, Skill):
            add(SkillEntry(source, inline=source))
        elif isinstance(source, _DirectorySkill):
            root = _directory(source.path)
            info, _ = _read(root)
            add(SkillEntry(info, root))
        else:
            root = _directory(Path(source))
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                resolved = child.resolve()
                if not resolved.is_relative_to(root):
                    raise ConfigError(f"Skill source escapes its configured directory: {child}")
                declaration = child / "SKILL.md"
                if declaration.exists() or declaration.is_symlink():
                    info, _ = _read(resolved)
                    add(SkillEntry(info, resolved))
    return _Catalog(tuple(entries.values()))


def _files(root: Path) -> list[str]:
    result = []
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = [
            name
            for name in children
            if not (path := Path(directory) / name).is_symlink() and not path.is_junction()
        ]
        for name in sorted(names):
            relative = (Path(directory) / name).relative_to(root).as_posix()
            try:
                _inside(root, relative)
            except (OSError, ValueError):
                continue
            result.append(relative)
    return sorted(result)


class _Loaded(BaseModel):
    model_config = ConfigDict(strict=True)
    name: SkillName
    instructions: str


class LoadedSkill(_Loaded):
    """The complete typed result of loading instructions and discovering resources."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    directory: Path | None = None
    files: tuple[str, ...] = ()


def _read_resource(root: Path, path: str) -> str:
    with _inside(root, path).open(encoding="utf-8", newline="") as stream:
        return stream.read()


class Skills(Plugin, slot="skills"):
    """Discover explicit sources once per session, then load instructions and files on demand."""

    def __init__(self, *sources: str | Path | Skill | _DirectorySkill):
        super().__init__()
        if any(not isinstance(source, str | Path | Skill | _DirectorySkill) for source in sources):
            raise ConfigError(
                "Skill sources must be folders, Skill.from_dir(...) or inline Skill values"
            )
        if any(not isinstance(source, Skill) for source in sources):
            _yaml()
        self.sources = tuple(sources)
        self._key = Key[_Catalog]("skill catalog")
        description = "; ".join(
            source.name
            if isinstance(source, Skill)
            else str(source.path)
            if isinstance(source, _DirectorySkill)
            else str(source)
            for source in sources
        )
        self.guidance = Section.from_factory(
            "skills",
            self._render_catalog,
            description=f"Skills: {description}; earlier sources win; read at session startup",
        )

    async def _catalog(self, ctx: Context) -> _Catalog:
        if self._key not in ctx:
            try:
                catalog = await asyncio.to_thread(_discover, self.sources)
            except ConfigError:
                raise
            except (OSError, ValueError, RuntimeError) as error:
                raise ConfigError(
                    f"Cannot read configured skill sources {self.sources!r}: {error}"
                ) from error
            ctx.provide(self._key, catalog)
        return ctx[self._key]

    async def _render_catalog(self, ctx: Context) -> str:
        catalog = await self._catalog(ctx)
        if not catalog.entries:
            return ""
        items = [
            f"<skill name={quoteattr(entry.info.name)}>{escape(entry.info.description)}</skill>"
            for entry in catalog.entries
        ]
        return (
            "Skills provide task-specific instructions. Before starting a matching task, "
            "call load_skill with its name and follow the instructions.\n\n<skills>\n"
            + "\n".join(items)
            + "\n</skills>"
        )

    async def setup(
        self, ctx: Context, session: Inject[SessionLog], context: Inject[DynamicContext]
    ):
        catalog = await self._catalog(ctx)
        ctx.provide(SkillCatalog, catalog)
        self._entries = {entry.info.name: entry for entry in catalog.entries}
        self._log, self._context = session, context
        self._calls: set[str] = set()
        self._loaded: dict[str, str] = {}
        for event in session.events:
            self._remember(event)
        if session.last(CompactionApplied) is not None:
            await self._retain()

    def _remember(self, event):
        if isinstance(event, ToolCalled):
            if event.call.name == "load_skill":
                self._calls.add(event.call.id)
            else:
                self._calls.discard(event.call.id)
        elif isinstance(event, ToolCompleted | ToolReturned) and event.call_id in self._calls:
            if event.result.status == "ok":
                try:
                    data, _ = json.JSONDecoder().raw_decode(event.result.text.lstrip())
                    loaded = _Loaded.model_validate(data)
                except (ValueError, TypeError):
                    pass
                else:
                    self._loaded[loaded.name] = loaded.instructions
            if isinstance(event, ToolReturned):
                self._calls.discard(event.call_id)

    async def _retain(self):
        for name, instructions in self._loaded.items():
            await self._context.set(f"skill:{name}", instructions)

    @hook(SessionAppended)
    async def recorded(self, event: SessionAppended):
        if event.log is not self._log:
            return
        self._remember(event.event)
        if isinstance(event.event, CompactionApplied):
            await self._retain()
        elif isinstance(event.event, ToolReturned):
            for name, instructions in self._loaded.items():
                if f"skill:{name}" in self._context.values:
                    await self._context.set(f"skill:{name}", instructions)

    def _entry(self, name: str) -> SkillEntry:
        if name not in self._entries:
            raise ToolError(f"Unknown skill: {name}")
        return self._entries[name]

    @tool(readonly=True, output_limit=None)
    async def load_skill(self, name: SkillName) -> LoadedSkill:
        """Load a skill's complete instructions, directory and resource file names."""
        entry = self._entry(name)
        if entry.inline is not None:
            return LoadedSkill(name=name, instructions=entry.inline.instructions)
        assert entry.directory is not None
        try:
            _, instructions = await asyncio.to_thread(_read, entry.directory, body=True)
            files = await asyncio.to_thread(_files, entry.directory)
            assert instructions is not None
            return LoadedSkill(
                name=name, instructions=instructions, directory=entry.directory, files=tuple(files)
            )
        except (ConfigError, OSError, ValueError) as error:
            raise ToolError(str(error)) from error

    @tool(readonly=True, output_limit=None)
    async def read_skill_file(self, name: SkillName, path: str) -> str:
        """Read a complete UTF-8 resource inside the named skill; never execute it."""
        entry = self._entry(name)
        if entry.directory is None:
            raise ToolError("Inline skills have no resource directory")
        try:
            return await asyncio.to_thread(_read_resource, entry.directory, path)
        except (OSError, ValueError, RuntimeError) as error:
            raise ToolError(f"Cannot read skill file: {error}") from error
