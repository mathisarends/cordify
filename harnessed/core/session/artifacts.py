import hashlib
from collections.abc import Iterable

from harnessed.core.errors import ConfigError
from harnessed.core.messages import SpillRef
from harnessed.core.session.events import (
    SessionEvent,
    SpillCreated,
    SpillExpirationReason,
    SpillExpired,
    ToolCompleted,
    ToolReturned,
)
from harnessed.core.session.files import SessionFiles, validate_file_key


def artifact_refs(events: Iterable[SessionEvent]) -> tuple[SpillRef, ...]:
    """Ignore expired references and invalid external paths without opening them."""
    active: dict[str, SpillRef] = {}
    expired: set[str] = set()
    for event in events:
        if isinstance(event, SpillExpired):
            expired.add(event.ref.path)
            active.pop(event.ref.path, None)
            continue
        if isinstance(event, SpillCreated):
            ref = event.ref
            expired.discard(ref.path)
        elif isinstance(event, ToolCompleted | ToolReturned) and event.result.spill is not None:
            ref = event.result.spill
            if ref.path in expired:
                continue
        else:
            continue
        try:
            validate_file_key(ref.path)
        except ValueError:
            continue
        active[ref.path] = ref
    return tuple(active.values())


async def copy_artifacts(
    events: Iterable[SessionEvent], source: SessionFiles | None, destination: SessionFiles
) -> tuple[SpillExpired, ...]:
    """Verify bytes before copying; missing content becomes an explicit branch event."""
    refs = artifact_refs(events)
    if not refs:
        return ()
    entries = {entry.key: entry for entry in await source.entries()} if source is not None else {}
    missing, copied = [], []
    try:
        for ref in refs:
            entry = entries.get(ref.path)
            if entry is None or source is None:
                missing.append(SpillExpired(ref, SpillExpirationReason.MISSING))
                continue
            pieces, offset, size = [], 0, 0
            digest = hashlib.sha256()
            try:
                while chunk := await source.read(ref.path, offset=offset, limit=16000):
                    data = chunk.encode("utf-8")
                    size += len(data)
                    if size > entry.size_bytes:
                        raise ConfigError(f"Fork artifact changed while copying: {ref.path}")
                    digest.update(data)
                    pieces.append(chunk)
                    offset += len(chunk)
            except FileNotFoundError:
                missing.append(SpillExpired(ref, SpillExpirationReason.MISSING))
                continue
            if size != entry.size_bytes or digest.hexdigest() != ref.sha256:
                raise ConfigError(f"Fork artifact integrity check failed: {ref.path}")
            # A cancellation-safe backend may acknowledge a put before re-raising cancellation.
            copied.append(ref.path)
            await destination.put(ref.path, "".join(pieces))
        return tuple(missing)
    except BaseException:
        for key in copied:
            await destination.remove(key)
        raise
