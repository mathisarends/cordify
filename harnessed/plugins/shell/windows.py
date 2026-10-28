"""Windows process ownership, established before the suspended process runs.

See https://learn.microsoft.com/windows/win32/procthread/job-objects.
The job denies breakaway and kills all descendants when its handle closes.
"""

import ctypes
from ctypes import wintypes

_api = ctypes.WinDLL("kernel32", use_last_error=True)


class _Limits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Thread(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("id", wintypes.DWORD),
        ("owner", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("delta", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


def _function(name, restype, *argtypes):
    fn = getattr(_api, name)
    fn.restype, fn.argtypes = restype, argtypes
    return fn


_create_job = _function("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
_set_job = _function(
    "SetInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
_assign = _function("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
_open_process = _function(
    "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
)
_snapshot = _function("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_first = _function("Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_Thread))
_next = _function("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_Thread))
_open_thread = _function(
    "OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
)
_resume = _function("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
_close = _function("CloseHandle", wintypes.BOOL, wintypes.HANDLE)


def _checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class WindowsJob:
    """Assign a suspended child to a kill-on-close job, then resume its thread."""

    def __init__(self):
        self.handle = _checked(_create_job(None, None))
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _checked(_set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def attach_and_resume(self, pid: int):
        process = _checked(_open_process(0x0101, False, pid))  # SET_QUOTA | TERMINATE
        try:
            _checked(_assign(self.handle, process))
        finally:
            _close(process)
        snapshot = _snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _Thread()
            entry.size = ctypes.sizeof(entry)
            found = _first(snapshot, ctypes.byref(entry))
            while found:
                if entry.owner == pid:
                    thread = _checked(_open_thread(0x0002, False, entry.id))
                    try:
                        if _resume(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        _close(thread)
                    return
                found = _next(snapshot, ctypes.byref(entry))
            raise OSError("The suspended process has no resumable thread")
        finally:
            _close(snapshot)

    def close(self):
        if self.handle:
            _close(self.handle)
            self.handle = None
