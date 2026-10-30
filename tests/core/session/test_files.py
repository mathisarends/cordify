import pytest
from pydantic import ValidationError

from harnessed.core.session.files import MemoryFiles


@pytest.fixture
def files(request, tmp_path):
    return MemoryFiles()


async def test_files_keep_exact_unicode_ranges_and_report_byte_sizes(files):
    original = "one\r\nä😀two\n"
    await files.put("spills/one.txt", original)
    assert await files.read("spills/one.txt", offset=5, limit=2) == "ä😀"
    assert await files.read("spills/one.txt", offset=100) == ""
    assert await files.read("spills/one.txt") == original
    (entry,) = await files.entries()
    assert entry.key == "spills/one.txt" and entry.size_bytes == len(original.encode("utf-8"))
    assert entry.modified_ns > 0
    await files.remove(entry.key)
    await files.remove(entry.key)
    assert not await files.entries()
    with pytest.raises(FileNotFoundError):
        await files.read(entry.key)


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "a/../../escape",
        "a\\escape",
        "NUL.txt",
        "spills/COM1",
        "name.",
        "spills/a.txt\n",
    ],
)
async def test_files_reject_escape_and_nonportable_keys(files, key):
    with pytest.raises(ValidationError):
        await files.put(key, "text")
    with pytest.raises(ValidationError):
        await files.read(key)
    with pytest.raises(ValidationError):
        await files.remove(key)


@pytest.mark.parametrize(
    "options", [{"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 16001}]
)
async def test_file_range_limits_are_strict(files, options):
    await files.put("file.txt", "value")
    with pytest.raises(ValidationError):
        await files.read("file.txt", **options)
