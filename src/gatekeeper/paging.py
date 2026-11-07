"""Whether the host was reading memory back from its disk (Gatekeeper#142).

On a host short of memory, what sits idle between visits (Whisper's code and buffers, the
conversation model's tables) is swapped out or dropped, and the next visit waits while
it's read back. Linux counts those reads in /proc/vmstat: ``pswpin``, pages read back from
swap, and ``pgmajfault``, the times a program waited on the disk (for swap or a file).
Counted around a piece of work they tell an answer that waited on the disk from a slow CPU
or GPU. The counts are the host's, not one program's: a container sees the host's
/proc/vmstat and /proc/meminfo.
"""

from __future__ import annotations

from pathlib import Path

VMSTAT = Path("/proc/vmstat")
MEMINFO = Path("/proc/meminfo")


def counters(path: Path = VMSTAT) -> dict[str, int]:
    """The host's swap-ins and disk waits so far, or {} where there's no /proc (Windows)."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name in ("pswpin", "pgmajfault"):
            out[name] = int(value)
    return out if len(out) == 2 else {}


def between(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """What happened between two ``counters()``: ``{"disk_waits": n, "swap_ins": n}``, or
    {} when either is missing."""
    if not before or not after:
        return {}
    return {
        "disk_waits": after["pgmajfault"] - before["pgmajfault"],
        "swap_ins": after["pswpin"] - before["pswpin"],
    }


def memory(path: Path = MEMINFO) -> dict | None:
    """The host's memory now, in MB: ``total``, ``available``, ``swap_total`` and
    ``swap_used``; None where there's no /proc."""
    try:
        text = path.read_text()
    except OSError:
        return None
    kb = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if (value := rest.split()) and value[0].isdigit():
            kb[name] = int(value[0])
    try:
        return {
            "total": kb["MemTotal"] // 1024,
            "available": kb["MemAvailable"] // 1024,
            "swap_total": kb["SwapTotal"] // 1024,
            "swap_used": (kb["SwapTotal"] - kb["SwapFree"]) // 1024,
        }
    except KeyError:
        return None


def describe(counts: dict) -> str:
    """``counts`` (``between()``) for a log line or the report: "waited on the disk 812
    times, 3104 pages from swap"."""
    return f"waited on the disk {counts['disk_waits']} times, {counts['swap_ins']} pages from swap"
