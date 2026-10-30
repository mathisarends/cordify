import fnmatch
import io
import os
import stat
import tarfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from .types import SandboxError, Workspace


def source_root(workspace: Workspace) -> Path:
    root = workspace.path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace source must be a directory")
    return root


def seed_archive(workspace: Workspace, maximum: int) -> bytes:
    """Build a bounded archive without following links, junctions or special files."""
    root = source_root(workspace)
    output = io.BytesIO()
    total = 0
    count = 0
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for directory, directories, files in os.walk(root, followlinks=False):
            current = Path(directory)
            kept: list[str] = []
            for name in sorted([*directories, *files]):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if any(
                    fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative, pattern)
                    for pattern in workspace.exclude
                ):
                    continue
                info = path.lstat()
                if path.is_symlink() or path.is_junction():
                    raise SandboxError(f"Workspace links are not supported: {relative}")
                count += 1
                if count > 10_000:
                    raise SandboxError("Workspace copy exceeds 10000 entries")
                entry = tarfile.TarInfo(relative)
                entry.mode = stat.S_IMODE(info.st_mode) & 0o777
                if stat.S_ISDIR(info.st_mode):
                    kept.append(name)
                    entry.type = tarfile.DIRTYPE
                    archive.addfile(entry)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > maximum:
                        raise SandboxError("Workspace copy exceeds max_transfer_bytes")
                    entry.size = info.st_size
                    with path.open("rb") as stream:
                        archive.addfile(entry, stream)
                else:
                    raise SandboxError(f"Workspace special files are not supported: {relative}")
                if output.tell() > maximum:
                    raise SandboxError("Workspace archive exceeds max_transfer_bytes")
            directories[:] = kept
    value = output.getvalue()
    if len(value) > maximum:
        raise SandboxError("Workspace archive exceeds max_transfer_bytes")
    return value


def export_file(destination: Path, data: bytes) -> None:
    """Replace exactly the application-selected destination; never extract an archive."""
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
