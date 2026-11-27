"""Process-group lifecycle tracking and bounded orphan reaping for agent CLIs.

Helios launches agent CLIs in their own process groups (`setsid` /
`start_new_session=True`) so cancelling or closing a chat cleans up nested
tool subprocesses too. Two failure modes can still strand those children:

1. On normal window close, asynchronous `GLib.timeout_add` timers and daemon
   reaper threads disappear as soon as the app exits, before any `SIGKILL`
   backstop can run; stopped (`T`/`t`) process groups also ignore `SIGTERM`
   until woken with `SIGCONT` or killed.
2. On an abrupt Helios crash, `_on_close_request` never runs at all, leaving
   live child process groups running behind broken stdio pipes. When Helios
   reopens and releases orphaned Work attempts, resuming the same chat would
   otherwise start a second agent editing the same workspace concurrently.

This module records every verified subprocess spawned by Helios under
`$HELIOS_STATE_DIR/live-children.json` keyed by kernel boot ID, Linux
`/proc/<pid>/stat` boot start ticks, a per-launch inherited group marker
(`HELIOS_CHILD_GROUP`), and snapshotted descendant identities so PID/PGID
reuse can never misidentify an unrelated process. All thread and file locks
are strictly deadline-bounded so a wedged sibling process can never hang
startup or shutdown. Clean shutdown reaps all tracked and registry-owned
groups in parallel inside one shared wall-clock budget, and startup reaps
verified orphans whose owning Helios PID is dead before execution lanes
reopen.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from helios.paths import bind_state_paths

# Dynamic state-dir path — resolves HELIOS_STATE_DIR at access time unless
# patched in tests.
_self = bind_state_paths(__name__, REGISTRY_PATH="live-children.json")

CHILD_GROUP_ENV = "HELIOS_CHILD_GROUP"

_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_ENVIRON_BYTES = 512 * 1024
_MAX_DESCENDANTS_PER_ENTRY = 256
_POLL_INTERVAL_SECONDS = 0.025
_LOCK_RETRY_SECONDS = 0.01
_DEFAULT_LOCK_TIMEOUT_SECONDS = 1.0
_INPROC_LOCK = threading.Lock()
_ABORT_LATCH_LOCK = threading.Lock()
_ABORT_LATCH_GEN = 0
_UNREAPED_ABORT_TARGETS: dict[
    tuple[int | None, int, int | None, str | None],
    tuple[int, ProcessTreeTarget],
] = {}


def _upsert_abort_latch_locked(target: ProcessTreeTarget) -> None:
    global _ABORT_LATCH_GEN
    key = _abort_latch_key(target)
    _ABORT_LATCH_GEN += 1
    existing = _UNREAPED_ABORT_TARGETS.get(key)
    if existing is None:
        _UNREAPED_ABORT_TARGETS[key] = (_ABORT_LATCH_GEN, target)
    else:
        _old_gen, prev = existing
        merged = replace(
            prev,
            poll=prev.poll or target.poll,
            provider=prev.provider or target.provider,
            owner_pid=prev.owner_pid or target.owner_pid,
            registry_path=prev.registry_path or target.registry_path,
            group_token=prev.group_token or target.group_token,
            descendants=_normalize_descendants(
                (*prev.descendants, *target.descendants)
            ),
        )
        _UNREAPED_ABORT_TARGETS[key] = (_ABORT_LATCH_GEN, merged)
_TOKEN_CHARS = frozenset("0123456789abcdefABCDEF-_")
_REAL_POPEN_TYPE = subprocess.Popen


def is_real_subprocess(proc: Any) -> bool:
    """Return True only for genuine OS subprocess handles, not test fakes."""
    if type(proc) is _REAL_POPEN_TYPE:
        return True
    cls = type(proc)
    return cls.__name__ == "Subprocess" and cls.__module__.startswith("gi.repository")

TreeStatus = Literal["live", "dead", "recycled", "unresolved"]


class ChildRegistryPersistenceError(RuntimeError):
    """Raised when a real spawned child cannot be durably recorded."""

    def __init__(
        self,
        message: str,
        *,
        child_identity: ProcessIdentity | None = None,
        descendants: Sequence[tuple[int, int]] = (),
    ) -> None:
        super().__init__(message)
        self.child_identity = child_identity
        self.descendants = _normalize_descendants(descendants)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Kernel-verified snapshot of one process from `/proc/<pid>/stat`."""

    pid: int
    state: str
    ppid: int
    pgid: int
    sid: int
    start_ticks: int
    uid: int


@dataclass(frozen=True, slots=True)
class ProcessTreeTarget:
    """One live child process (and optional owned process group) to reap."""

    pid: int
    pgid: int | None
    start_ticks: int | None = None
    poll: Callable[[], Any] | None = None
    provider: str = ""
    owner_pid: int = 0
    registry_path: Path | None = None
    group_token: str | None = None
    descendants: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ReapResult:
    """Outcome of a bounded parallel process-tree reap."""

    reaped: tuple[ProcessTreeTarget, ...]
    remaining: tuple[ProcessTreeTarget, ...]
    unresolved: tuple[ProcessTreeTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class OrphanReapOutcome:
    """Explicit outcome of reaping registered child process groups."""

    reaped: tuple[dict[str, Any], ...] = ()
    remaining: tuple[dict[str, Any], ...] = ()
    unresolved: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.remaining and not self.unresolved


def new_group_token() -> str:
    """Generate a unique non-secret per-launch process-group marker."""
    return secrets.token_hex(16)


def _is_pos_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_group_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 128
        and all(ch in _TOKEN_CHARS for ch in value)
    )


def _normalize_descendants(
    descendants: Sequence[tuple[int, int]] | None,
    *,
    limit: int | None = None,
) -> tuple[tuple[int, int], ...]:
    if not descendants:
        return ()
    seen: set[tuple[int, int]] = set()
    cleaned: list[tuple[int, int]] = []
    for item in descendants:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        dpid, dticks = item[0], item[1]
        if not _is_pos_pid(dpid) or not _is_nonneg_int(dticks):
            continue
        pair = (dpid, dticks)
        if pair in seen:
            continue
        seen.add(pair)
        cleaned.append(pair)
        if limit is not None and len(cleaned) >= limit:
            break
    return tuple(cleaned)


def _prune_live_descendants_for_storage(
    descendants: Sequence[tuple[int, int]],
) -> tuple[list[list[int]], tuple[tuple[int, int], ...]]:
    """Keep live verified descendants for JSON persistence, returning `(stored, overflow)`."""
    my_uid = _current_uid()
    my_pid = os.getpid()
    live: list[tuple[int, int]] = []
    for d_pid, d_ticks in _normalize_descendants(descendants):
        ident = read_process_identity(d_pid)
        if (
            ident is not None
            and ident.uid == my_uid
            and ident.pid != my_pid
            and ident.start_ticks == d_ticks
            and ident.state != "Z"
        ):
            live.append((d_pid, d_ticks))
    stored = [[dp, dt] for dp, dt in live[:_MAX_DESCENDANTS_PER_ENTRY]]
    overflow = tuple(live[_MAX_DESCENDANTS_PER_ENTRY:])
    return stored, overflow


def current_boot_id() -> str | None:
    """Return Linux `/proc/sys/kernel/random/boot_id`, or `None` if unavailable."""
    try:
        with open(
            "/proc/sys/kernel/random/boot_id", encoding="utf-8", errors="replace"
        ) as fh:
            raw = fh.read(128).strip()
    except OSError:
        return None
    return raw or None


def read_process_identity(pid: int) -> ProcessIdentity | None:
    """Read Linux `/proc/<pid>/stat` and UID; returns `None` if unavailable."""
    if not _is_pos_pid(pid):
        return None
    proc_dir = f"/proc/{pid}"
    stat_path = f"{proc_dir}/stat"
    try:
        uid = os.stat(proc_dir).st_uid
        with open(stat_path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read(4096)
    except OSError:
        return None

    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    tail = raw[rparen + 1 :].strip().split()
    # After `(comm)`, tail[0] is field 3 (state), tail[1] is field 4 (ppid),
    # tail[2] is field 5 (pgrp), tail[3] is field 6 (session),
    # tail[19] is field 22 (starttime in clock ticks since boot).
    if len(tail) < 20:
        return None
    try:
        state = tail[0]
        ppid = int(tail[1])
        pgid = int(tail[2])
        sid = int(tail[3])
        start_ticks = int(tail[19])
    except ValueError:
        return None
    return ProcessIdentity(
        pid=pid,
        state=state,
        ppid=ppid,
        pgid=pgid,
        sid=sid,
        start_ticks=start_ticks,
        uid=uid,
    )


def wait_for_process_group(
    pid: int | None,
    *,
    expected_new_session: bool,
    timeout: float = 0.5,
) -> int | None:
    """Wait boundedly for a `setsid`-wrapped child to form its own session/PGID.

    Returns the verified PGID (`pgid == sid == pid != parent_pgrp`), or `None`
    if not expected, if the process already exited, or if the timeout elapsed.
    """
    if not expected_new_session or not _is_pos_pid(pid):
        return None
    assert isinstance(pid, int)
    parent_group = _current_pgrp()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        ident = read_process_identity(pid)
        if ident is not None:
            if ident.state == "Z":
                return None
            if (
                ident.pgid > 1
                and ident.pgid == pid
                and ident.sid == ident.pgid
                and ident.pgid != parent_group
            ):
                return ident.pgid
        else:
            try:
                pgid = os.getpgid(pid)
                sid = os.getsid(pid)
            except (OSError, ProcessLookupError):
                return None
            if pgid > 1 and pgid == pid and sid == pgid and pgid != parent_group:
                return pgid
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.005, remaining))


def scan_token_members(group_token: str | None) -> list[ProcessIdentity]:
    """Find live same-UID processes carrying exact `HELIOS_CHILD_GROUP` token."""
    if not _is_valid_group_token(group_token):
        return []
    assert isinstance(group_token, str)
    my_pid = os.getpid()
    my_uid = _current_uid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    found: list[ProcessIdentity] = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid == my_pid:
            continue
        if not _process_has_group_token(pid, group_token):
            continue
        ident = read_process_identity(pid)
        if ident is not None and ident.uid == my_uid and ident.state != "Z":
            found.append(ident)
    return found


def _snapshot_token_descendants(
    group_token: str | None,
    *,
    exclude_pid: int | None = None,
) -> tuple[tuple[int, int], ...]:
    """Collect live token-verified members and any children in escaped token groups."""
    members = scan_token_members(group_token)
    if not members:
        return ()
    my_pgid = _current_pgrp()
    pairs: list[tuple[int, int]] = []
    for member in members:
        if member.pid != exclude_pid:
            pairs.append((member.pid, member.start_ticks))
        if (
            member.pgid == member.pid
            and _is_pos_pid(member.pgid)
            and member.pgid != my_pgid
        ):
            for gm_pid, gm_ticks in snapshot_group_descendants(
                member.pgid, member.pid, member.start_ticks
            ):
                if gm_pid != exclude_pid:
                    pairs.append((gm_pid, gm_ticks))
    return _normalize_descendants(pairs)


def _process_has_group_token(pid: int, group_token: str) -> bool:
    """Check whether `/proc/<pid>/environ` carries exact `HELIOS_CHILD_GROUP`."""
    if not _is_pos_pid(pid) or not _is_valid_group_token(group_token):
        return False
    needle = f"{CHILD_GROUP_ENV}={group_token}".encode("ascii")
    try:
        fd = os.open(
            f"/proc/{pid}/environ",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return False
    try:
        chunks: list[bytes] = []
        remaining = _MAX_ENVIRON_BYTES
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    raw = b"".join(chunks)
    if not raw:
        return False
    return needle in raw.split(b"\x00")


def _current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _current_pgrp() -> int:
    try:
        return os.getpgrp()
    except OSError:
        return 0


def _owner_is_dead(owner_pid: int, owner_start_ticks: int) -> bool:
    """Return True when the recorded owning Helios process has exited."""
    ident = read_process_identity(owner_pid)
    if ident is None:
        return True
    if ident.uid != _current_uid():
        return True
    return ident.start_ticks != owner_start_ticks or ident.state == "Z"


def _scan_group_members(pgid: int) -> list[ProcessIdentity] | None:
    """List current `/proc` processes whose PGID equals `pgid`."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    members: list[ProcessIdentity] = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1:
            continue
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                raw = fh.read(4096)
        except OSError:
            continue
        rparen = raw.rfind(")")
        if rparen < 0:
            continue
        tail = raw[rparen + 1 :].strip().split()
        if len(tail) < 20:
            continue
        try:
            if int(tail[2]) != pgid:
                continue
        except ValueError:
            continue
        ident = read_process_identity(pid)
        if ident is not None and ident.pgid == pgid:
            members.append(ident)
    return members


def snapshot_group_descendants(
    pgid: int | None,
    leader_pid: int,
    leader_start_ticks: int | None,
) -> tuple[tuple[int, int], ...]:
    """Capture live descendant `(pid, start_ticks)` while the leader is verified."""
    if (
        not _is_pos_pid(leader_pid)
        or not _is_pos_pid(pgid)
        or pgid != leader_pid
        or pgid == _current_pgrp()
        or not _is_nonneg_int(leader_start_ticks)
    ):
        return ()
    my_uid = _current_uid()
    my_pid = os.getpid()
    leader = read_process_identity(leader_pid)
    if (
        leader is None
        or leader.uid != my_uid
        or leader.start_ticks != leader_start_ticks
        or leader.pgid != pgid
    ):
        return ()
    members = _scan_group_members(pgid)
    if not members:
        return ()
    captured: list[tuple[int, int]] = []
    for member in members:
        if member.pid == leader_pid or member.pid == my_pid:
            continue
        if member.uid != my_uid or member.state == "Z":
            continue
        if member.start_ticks < leader_start_ticks:
            continue
        captured.append((member.pid, member.start_ticks))
    return tuple(captured)


def _classify_group_detailed(
    pgid: int,
    leader_pid: int,
    leader_start_ticks: int,
    *,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> tuple[TreeStatus, tuple[tuple[int, int], ...]]:
    """Classify an owned process group and return any verified live member identities."""
    my_pid = os.getpid()
    my_uid = _current_uid()
    my_pgid = _current_pgrp()
    if not _is_pos_pid(pgid) or pgid == my_pgid or pgid != leader_pid:
        return "dead", ()

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return "dead", ()
    except PermissionError:
        return "unresolved", ()
    except OSError:
        return "unresolved", ()

    leader = read_process_identity(leader_pid)
    if leader is not None:
        if leader.uid != my_uid or leader.start_ticks != leader_start_ticks:
            return "recycled", ()
        if leader.pgid != pgid:
            return "dead", ()
        members = _scan_group_members(pgid)
        if members is None:
            return "unresolved", ()
        live_members: list[ProcessIdentity] = []
        for member in members:
            if member.uid != my_uid or member.pid == my_pid:
                return "unresolved", ()
            if member.start_ticks < leader_start_ticks:
                return "unresolved", ()
            if member.state != "Z":
                live_members.append(member)
        if not live_members:
            return "dead", ()
        discovered = _normalize_descendants(
            [(m.pid, m.start_ticks) for m in live_members if m.pid != leader_pid]
        )
        return "live", discovered

    # Leaderless PGID: require positive proof of continuity (recorded descendant
    # identity or per-launch HELIOS_CHILD_GROUP marker).
    members = _scan_group_members(pgid)
    if members is None:
        return "unresolved", ()
    live_members = [member for member in members if member.state != "Z"]
    if not live_members:
        return "dead", ()
    for member in members:
        if member.uid != my_uid or member.pid == my_pid:
            return "recycled", ()
        if member.start_ticks < leader_start_ticks:
            return "recycled", ()
        if member.pid == pgid and member.start_ticks != leader_start_ticks:
            return "recycled", ()

    desc_set = set(_normalize_descendants(expected_descendants))
    verified_by_desc = bool(
        desc_set and any((m.pid, m.start_ticks) in desc_set for m in live_members)
    )
    verified_by_token = bool(
        not verified_by_desc
        and _is_valid_group_token(expected_group_token)
        and any(
            _process_has_group_token(m.pid, expected_group_token)  # type: ignore[arg-type]
            for m in live_members
        )
    )
    if verified_by_desc or verified_by_token:
        discovered = _normalize_descendants(
            [(m.pid, m.start_ticks) for m in live_members if m.pid != leader_pid]
        )
        return "live", discovered
    return "unresolved", ()


def _classify_group(
    pgid: int,
    leader_pid: int,
    leader_start_ticks: int,
    *,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> TreeStatus:
    """Classify an owned process group without trusting unverified leaderless PGIDs."""
    status, _ = _classify_group_detailed(
        pgid,
        leader_pid,
        leader_start_ticks,
        expected_descendants=expected_descendants,
        expected_group_token=expected_group_token,
    )
    return status


def _classify_process_tree_detailed(
    pgid: int | None,
    pid: int,
    *,
    expected_start_ticks: int | None = None,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> tuple[TreeStatus, tuple[tuple[int, int], ...]]:
    """Classify a process tree and return any newly discovered token descendants."""
    if not _is_pos_pid(pid):
        return "dead", ()
    my_uid = _current_uid()
    my_pgid = _current_pgrp()
    safe_pgid = pgid if (_is_pos_pid(pgid) and pgid != my_pgid) else None

    if expected_start_ticks is not None:
        status: TreeStatus
        group_discovered: tuple[tuple[int, int], ...] = ()
        ident = read_process_identity(pid)
        if ident is not None:
            if ident.uid != my_uid or ident.start_ticks != expected_start_ticks:
                status = "recycled"
            elif ident.state != "Z":
                return "live", ()
            elif safe_pgid is not None and safe_pgid == pid:
                status, group_discovered = _classify_group_detailed(
                    safe_pgid,
                    pid,
                    expected_start_ticks,
                    expected_descendants=expected_descendants,
                    expected_group_token=expected_group_token,
                )
            else:
                status = "dead"
        elif safe_pgid is not None and safe_pgid == pid:
            status, group_discovered = _classify_group_detailed(
                safe_pgid,
                pid,
                expected_start_ticks,
                expected_descendants=expected_descendants,
                expected_group_token=expected_group_token,
            )
        else:
            status = "dead"
        if status == "live":
            return "live", group_discovered
        if status in ("dead", "recycled"):
            for d_pid, d_ticks in _normalize_descendants(expected_descendants):
                d_ident = read_process_identity(d_pid)
                if (
                    d_ident is not None
                    and d_ident.uid == my_uid
                    and d_ident.start_ticks == d_ticks
                    and d_ident.state != "Z"
                ):
                    return "live", ()
            if _is_valid_group_token(expected_group_token):
                found = scan_token_members(expected_group_token)
                if found:
                    pairs: list[tuple[int, int]] = []
                    for m in found:
                        if m.pid != pid:
                            pairs.append((m.pid, m.start_ticks))
                        if (
                            m.pgid == m.pid
                            and _is_pos_pid(m.pgid)
                            and m.pgid != my_pgid
                        ):
                            for gm_pid, gm_ticks in snapshot_group_descendants(
                                m.pgid, m.pid, m.start_ticks
                            ):
                                if gm_pid != pid:
                                    pairs.append((gm_pid, gm_ticks))
                    return "live", _normalize_descendants(pairs)
        return status, ()

    ident = read_process_identity(pid)
    if ident is not None:
        if ident.state != "Z":
            return "live", ()
        if safe_pgid is not None:
            members = _scan_group_members(safe_pgid)
            if members is not None:
                return (
                    (
                        "live"
                        if any(m.state != "Z" and m.uid == my_uid for m in members)
                        else "dead"
                    ),
                    (),
                )
        return "dead", ()

    if safe_pgid is not None:
        try:
            os.killpg(safe_pgid, 0)
            return "live", ()
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return "live", ()
    except ProcessLookupError:
        return "dead", ()
    except OSError:
        return "dead", ()


def classify_process_tree(
    pgid: int | None,
    pid: int,
    *,
    expected_start_ticks: int | None = None,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> TreeStatus:
    """Classify a process (and optional owned process group) conservatively."""
    status, _ = _classify_process_tree_detailed(
        pgid,
        pid,
        expected_start_ticks=expected_start_ticks,
        expected_descendants=expected_descendants,
        expected_group_token=expected_group_token,
    )
    return status


def inspect_process_tree(
    target: ProcessTreeTarget,
) -> tuple[TreeStatus, ProcessTreeTarget]:
    """Refresh verified descendants and classify `target` without dropping discoveries."""
    refreshed = _refresh_target_descendants(target)
    status, discovered = _classify_process_tree_detailed(
        refreshed.pgid,
        refreshed.pid,
        expected_start_ticks=refreshed.start_ticks,
        expected_descendants=refreshed.descendants,
        expected_group_token=refreshed.group_token,
    )
    if discovered:
        merged = _normalize_descendants((*refreshed.descendants, *discovered))
        if merged != refreshed.descendants:
            refreshed = replace(refreshed, descendants=merged)
    return status, refreshed


def is_process_tree_live(
    pgid: int | None,
    pid: int,
    *,
    expected_start_ticks: int | None = None,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> bool:
    """Return True only when the verified process (or owned group) is live."""
    return (
        classify_process_tree(
            pgid,
            pid,
            expected_start_ticks=expected_start_ticks,
            expected_descendants=expected_descendants,
            expected_group_token=expected_group_token,
        )
        == "live"
    )


def signal_process_tree(
    pgid: int | None,
    pid: int,
    sig: signal.Signals,
    *,
    wake_stopped: bool = False,
    expected_start_ticks: int | None = None,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
    discovered_out: list[tuple[int, int]] | None = None,
) -> bool:
    """Send `sig` to a verified process group (or PID), refusing unverified groups."""
    if not _is_pos_pid(pid):
        return False
    my_uid = _current_uid()
    my_pgid = _current_pgrp()
    safe_pgid = pgid if (_is_pos_pid(pgid) and pgid != my_pgid) else None

    target_pgid: int | None = None
    target_pid: int | None = None

    if expected_start_ticks is not None:
        ident = read_process_identity(pid)
        if ident is not None:
            if ident.uid == my_uid and ident.start_ticks == expected_start_ticks:
                if (
                    safe_pgid is not None
                    and safe_pgid == pid
                    and ident.pgid == safe_pgid
                    and _classify_group(
                        safe_pgid,
                        pid,
                        expected_start_ticks,
                        expected_descendants=expected_descendants,
                        expected_group_token=expected_group_token,
                    )
                    == "live"
                ):
                    target_pgid = safe_pgid
                elif ident.state != "Z":
                    target_pid = pid
        elif safe_pgid is not None and safe_pgid == pid:
            if (
                _classify_group(
                    safe_pgid,
                    pid,
                    expected_start_ticks,
                    expected_descendants=expected_descendants,
                    expected_group_token=expected_group_token,
                )
                == "live"
            ):
                target_pgid = safe_pgid
    else:
        if safe_pgid is not None:
            target_pgid = safe_pgid
        else:
            target_pid = pid

    delivered = False
    signalled_pgids: set[int] = set()
    signalled_pids: set[int] = set()
    if target_pgid is not None:
        try:
            os.killpg(target_pgid, sig)
            delivered = True
            signalled_pgids.add(target_pgid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if wake_stopped and sig == signal.SIGTERM and target_pgid in signalled_pgids:
            try:
                os.killpg(target_pgid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if not delivered and target_pid is None and expected_start_ticks is None:
            target_pid = pid

    if target_pid is not None:
        try:
            os.kill(target_pid, sig)
            delivered = True
            signalled_pids.add(target_pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if wake_stopped and sig == signal.SIGTERM and target_pid in signalled_pids:
            try:
                os.kill(target_pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    extra_members: list[ProcessIdentity] = []
    seen_extra_pids: set[int] = set()
    for d_pid, d_ticks in _normalize_descendants(expected_descendants):
        d_ident = read_process_identity(d_pid)
        if (
            d_ident is not None
            and d_ident.uid == my_uid
            and d_ident.pid != os.getpid()
            and d_ident.start_ticks == d_ticks
            and d_ident.state != "Z"
        ):
            extra_members.append(d_ident)
            seen_extra_pids.add(d_ident.pid)
    if _is_valid_group_token(expected_group_token):
        for member in scan_token_members(expected_group_token):
            if member.pid not in seen_extra_pids:
                extra_members.append(member)
                seen_extra_pids.add(member.pid)
            if discovered_out is not None and member.pid != pid:
                discovered_out.append((member.pid, member.start_ticks))
    # Snapshot every verified escaped group leader's members BEFORE signalling so
    # token-scrubbed children inside escaped groups are retained if their leader exits.
    if extra_members:
        for member in list(extra_members):
            if (
                member.pgid == member.pid
                and _is_pos_pid(member.pgid)
                and member.pgid != my_pgid
            ):
                for gm_pid, gm_ticks in snapshot_group_descendants(
                    member.pgid, member.pid, member.start_ticks
                ):
                    if discovered_out is not None and gm_pid != pid:
                        discovered_out.append((gm_pid, gm_ticks))
                    if gm_pid not in seen_extra_pids:
                        gm_ident = read_process_identity(gm_pid)
                        if (
                            gm_ident is not None
                            and gm_ident.uid == my_uid
                            and gm_ident.pid != os.getpid()
                            and gm_ident.start_ticks == gm_ticks
                            and gm_ident.state != "Z"
                        ):
                            extra_members.append(gm_ident)
                            seen_extra_pids.add(gm_ident.pid)
        for member in extra_members:
            if member.pgid in signalled_pgids or member.pid in signalled_pids:
                continue
            if (
                member.pgid == member.pid
                and _is_pos_pid(member.pgid)
                and member.pgid != my_pgid
            ):
                try:
                    os.killpg(member.pgid, sig)
                    delivered = True
                    signalled_pgids.add(member.pgid)
                    if wake_stopped and sig == signal.SIGTERM:
                        try:
                            os.killpg(member.pgid, signal.SIGCONT)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            else:
                try:
                    os.kill(member.pid, sig)
                    delivered = True
                    signalled_pids.add(member.pid)
                    if wake_stopped and sig == signal.SIGTERM:
                        try:
                            os.kill(member.pid, signal.SIGCONT)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    return delivered


def wake_if_stopped(
    pgid: int | None,
    pid: int,
    *,
    expected_start_ticks: int | None = None,
    expected_descendants: Sequence[tuple[int, int]] = (),
    expected_group_token: str | None = None,
) -> bool:
    """Send `SIGCONT` only when the verified child or group has a stopped (`T`/`t`) task."""
    ident = read_process_identity(pid)
    my_uid = _current_uid()
    stopped = False
    if ident is not None:
        if ident.uid != my_uid:
            return False
        if expected_start_ticks is not None and ident.start_ticks != expected_start_ticks:
            return False
        if ident.state in ("T", "t"):
            stopped = True
    if not stopped and _is_pos_pid(pgid) and pgid == pid and pgid != _current_pgrp():
        if expected_start_ticks is not None:
            if (
                _classify_group(
                    pgid,
                    pid,
                    expected_start_ticks,
                    expected_descendants=expected_descendants,
                    expected_group_token=expected_group_token,
                )
                != "live"
            ):
                return False
        members = _scan_group_members(pgid)
        if members and any(m.uid == my_uid and m.state in ("T", "t") for m in members):
            stopped = True
    if not stopped:
        return False
    return signal_process_tree(
        pgid,
        pid,
        signal.SIGCONT,
        expected_start_ticks=expected_start_ticks,
        expected_descendants=expected_descendants,
        expected_group_token=expected_group_token,
    )


def capture_child_identity(
    pid: int,
    *,
    pgid: int | None = None,
    own_group: bool = False,
) -> ProcessIdentity | None:
    """Verify that `pid` is a real direct child of this Helios process."""
    if not _is_pos_pid(pid):
        return None
    ident = read_process_identity(pid)
    if ident is None:
        return None
    if ident.uid != _current_uid() or ident.ppid != os.getpid():
        return None
    if own_group:
        expected_pgid = pgid if _is_pos_pid(pgid) else ident.pgid
        if expected_pgid <= 1 or expected_pgid == _current_pgrp():
            return None
        if ident.pgid != expected_pgid:
            return None
    return ident


def _abort_latch_key(
    target: ProcessTreeTarget,
) -> tuple[int | None, int, int | None, str | None]:
    return (target.pgid, target.pid, target.start_ticks, target.group_token)


def has_uncleaned_aborts() -> bool:
    with _ABORT_LATCH_LOCK:
        return bool(_UNREAPED_ABORT_TARGETS)


def incomplete_cleanup_reason() -> str:
    with _ABORT_LATCH_LOCK:
        count = len(_UNREAPED_ABORT_TARGETS)
    if count <= 0:
        return ""
    return (
        f"Helios could not confirm {count} aborted launch process(es) stopped. "
        "New agent runs remain blocked until cleanup succeeds."
    )


def retry_uncleaned_aborts(*, timeout: float = 0.25) -> bool:
    """Retry reaping any survivors latched by failed launch-abort cleanup.

    Uses per-entry generation compare-and-remove so discoveries latched
    concurrently while a retry is running are never dropped.
    """
    with _ABORT_LATCH_LOCK:
        items = list(_UNREAPED_ABORT_TARGETS.items())
    if not items:
        return True
    cleared_gens: dict[tuple[int | None, int, int | None, str | None], int] = {}
    refreshed_blocked: list[ProcessTreeTarget] = []
    for key, (gen, target) in items:
        cleared, remaining = _abort_unregistered_child_detailed(
            target.pid,
            pgid=target.pgid,
            start_ticks=target.start_ticks,
            group_token=target.group_token,
            descendants=target.descendants,
            poll=target.poll,
            timeout=min(0.05, timeout),
            kill_wait=timeout,
        )
        if cleared:
            cleared_gens[key] = gen
        else:
            refreshed_blocked.extend(remaining)
    with _ABORT_LATCH_LOCK:
        for key, expected_gen in cleared_gens.items():
            cur = _UNREAPED_ABORT_TARGETS.get(key)
            if cur is not None and cur[0] == expected_gen:
                _UNREAPED_ABORT_TARGETS.pop(key, None)
        for rem_target in refreshed_blocked:
            _upsert_abort_latch_locked(rem_target)
        return not _UNREAPED_ABORT_TARGETS


def _abort_unregistered_child_detailed(
    pid: int | None,
    *,
    pgid: int | None = None,
    start_ticks: int | None = None,
    group_token: str | None = None,
    descendants: Sequence[tuple[int, int]] = (),
    poll: Callable[[], Any] | None = None,
    timeout: float = 0.05,
    kill_wait: float = 0.25,
) -> tuple[bool, list[ProcessTreeTarget]]:
    my_pgid = _current_pgrp()
    my_uid = _current_uid()
    token = group_token if _is_valid_group_token(group_token) else None
    preserved_desc = _normalize_descendants(descendants)

    # Never adopt the current occupant's start_ticks when the caller did not
    # preserve the original captured identity: if a token is available, rely on
    # token-verified members (plus preserved descendants) instead; otherwise
    # require a direct-child ppid match.
    targets: list[ProcessTreeTarget] = []
    if _is_pos_pid(pid):
        assert isinstance(pid, int)
        verified_ticks: int | None = None
        safe_pgid: int | None = None
        if start_ticks is not None:
            verified_ticks = start_ticks
            ident = read_process_identity(pid)
            if _is_pos_pid(pgid) and pgid == pid and pgid != my_pgid:
                safe_pgid = pgid
            elif (
                ident is not None
                and ident.start_ticks == start_ticks
                and ident.pgid == pid
                and ident.pgid != my_pgid
            ):
                safe_pgid = ident.pgid
        elif token is None:
            direct = capture_child_identity(pid, own_group=False)
            if direct is not None and direct.state != "Z":
                verified_ticks = direct.start_ticks
                if _is_pos_pid(pgid) and pgid == pid and pgid != my_pgid and direct.pgid == pgid:
                    safe_pgid = pgid
                elif direct.pgid == pid and direct.pgid != my_pgid:
                    safe_pgid = direct.pgid
        if verified_ticks is not None:
            snap_desc = (
                snapshot_group_descendants(safe_pgid, pid, verified_ticks)
                if safe_pgid is not None
                else ()
            )
            merged_desc = _normalize_descendants((*preserved_desc, *snap_desc))
            targets.append(
                ProcessTreeTarget(
                    pid=pid,
                    pgid=safe_pgid,
                    start_ticks=verified_ticks,
                    poll=poll,
                    group_token=token,
                    descendants=merged_desc,
                )
            )

    result = ReapResult(reaped=(), remaining=(), unresolved=())
    if targets:
        result = reap_process_trees(
            targets,
            timeout=timeout,
            kill_wait=kill_wait,
            send_term=True,
            unregister=False,
        )

    # Catch any descendant that escaped PGID (and preserve any incoming
    # verified descendants across transient /proc/<pid>/environ exec gaps).
    tracked_survivors: dict[tuple[int, int], ProcessTreeTarget] = {}
    for d_pid, d_ticks in preserved_desc:
        d_ident = read_process_identity(d_pid)
        if (
            d_ident is not None
            and d_ident.uid == my_uid
            and d_ident.pid != os.getpid()
            and d_ident.start_ticks == d_ticks
            and d_ident.state != "Z"
        ):
            d_pgid = (
                d_ident.pgid
                if d_ident.pgid == d_ident.pid and d_ident.pgid != my_pgid
                else None
            )
            tracked_survivors[(d_pid, d_ticks)] = ProcessTreeTarget(
                pid=d_pid,
                pgid=d_pgid,
                start_ticks=d_ticks,
                group_token=token,
            )
            if d_pgid is not None:
                for gm_pid, gm_ticks in snapshot_group_descendants(
                    d_pgid, d_pid, d_ticks
                ):
                    gm_key = (gm_pid, gm_ticks)
                    if gm_key not in tracked_survivors:
                        tracked_survivors[gm_key] = ProcessTreeTarget(
                            pid=gm_pid,
                            pgid=None,
                            start_ticks=gm_ticks,
                            group_token=token,
                        )

    if token is not None or tracked_survivors:
        sweep_deadline = time.monotonic() + max(0.05, float(kill_wait))
        for _ in range(3):
            if token is not None:
                for s in scan_token_members(token):
                    s_key = (s.pid, s.start_ticks)
                    s_pgid = s.pgid if s.pgid == s.pid and s.pgid != my_pgid else None
                    if s_key not in tracked_survivors:
                        tracked_survivors[s_key] = ProcessTreeTarget(
                            pid=s.pid,
                            pgid=s_pgid,
                            start_ticks=s.start_ticks,
                            group_token=token,
                        )
                    if s_pgid is not None:
                        for gm_pid, gm_ticks in snapshot_group_descendants(
                            s_pgid, s.pid, s.start_ticks
                        ):
                            gm_key = (gm_pid, gm_ticks)
                            if gm_key not in tracked_survivors:
                                tracked_survivors[gm_key] = ProcessTreeTarget(
                                    pid=gm_pid,
                                    pgid=None,
                                    start_ticks=gm_ticks,
                                    group_token=token,
                                )
            live_survivors = []
            for s_key, s_target in list(tracked_survivors.items()):
                s_ident = read_process_identity(s_target.pid)
                if (
                    s_ident is not None
                    and s_ident.uid == my_uid
                    and s_ident.start_ticks == s_target.start_ticks
                    and s_ident.state != "Z"
                ):
                    live_survivors.append(s_target)
                else:
                    tracked_survivors.pop(s_key, None)
            if not live_survivors:
                break
            rem = max(0.0, sweep_deadline - time.monotonic())
            reap_process_trees(
                live_survivors,
                timeout=0.0,
                kill_wait=rem,
                send_term=False,
                unregister=False,
            )
            for s_key, s_target in list(tracked_survivors.items()):
                s_ident = read_process_identity(s_target.pid)
                if not (
                    s_ident is not None
                    and s_ident.uid == my_uid
                    and s_ident.start_ticks == s_target.start_ticks
                    and s_ident.state != "Z"
                ):
                    tracked_survivors.pop(s_key, None)
            if token is not None:
                for s in scan_token_members(token):
                    s_key = (s.pid, s.start_ticks)
                    s_pgid = s.pgid if s.pgid == s.pid and s.pgid != my_pgid else None
                    if s_key not in tracked_survivors:
                        tracked_survivors[s_key] = ProcessTreeTarget(
                            pid=s.pid,
                            pgid=s_pgid,
                            start_ticks=s.start_ticks,
                            group_token=token,
                        )
                    if s_pgid is not None:
                        for gm_pid, gm_ticks in snapshot_group_descendants(
                            s_pgid, s.pid, s.start_ticks
                        ):
                            gm_key = (gm_pid, gm_ticks)
                            if gm_key not in tracked_survivors:
                                tracked_survivors[gm_key] = ProcessTreeTarget(
                                    pid=gm_pid,
                                    pgid=None,
                                    start_ticks=gm_ticks,
                                    group_token=token,
                                )
            if not tracked_survivors or time.monotonic() >= sweep_deadline:
                break

    remaining_survivors = list(tracked_survivors.values())
    all_remaining = list(result.remaining) + list(result.unresolved) + remaining_survivors
    cleared = not all_remaining
    return cleared, all_remaining


def abort_unregistered_child(
    pid: int | None,
    *,
    pgid: int | None = None,
    start_ticks: int | None = None,
    group_token: str | None = None,
    descendants: Sequence[tuple[int, int]] = (),
    poll: Callable[[], Any] | None = None,
    timeout: float = 0.05,
    kill_wait: float = 0.25,
    _record_latch: bool = True,
) -> bool:
    """Terminate and reap a newly spawned child and any verified descendants.

    Returns True only when no verified target or survivor remains alive;
    otherwise latches all remaining refreshed targets so execution stays blocked.
    """
    cleared, remaining = _abort_unregistered_child_detailed(
        pid,
        pgid=pgid,
        start_ticks=start_ticks,
        group_token=group_token,
        descendants=descendants,
        poll=poll,
        timeout=timeout,
        kill_wait=kill_wait,
    )
    if not cleared and _record_latch:
        with _ABORT_LATCH_LOCK:
            for item in remaining:
                _upsert_abort_latch_locked(item)
    return cleared


def _resolve_registry_path(registry_path: Path | None) -> Path:
    return (
        Path(registry_path)
        if registry_path is not None
        else Path(_self.REGISTRY_PATH)
    )


def _acquire_inproc_lock(deadline: float) -> bool:
    remaining = max(0.0, deadline - time.monotonic())
    return _INPROC_LOCK.acquire(timeout=remaining)


def _open_lock(
    registry_path: Path,
    *,
    deadline: float,
) -> tuple[int | None, str | None]:
    parent = registry_path.parent
    try:
        existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if not existed:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        lock_path = parent / f".{registry_path.name}.lock"
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        return None, f"could not open child registry lock: {exc}"

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None, "child registry lock is not a regular file"
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, None
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(fd)
                    return None, f"could not lock child registry: {exc}"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.close(fd)
                    return None, "timed out waiting for child registry lock"
                time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, f"could not lock child registry: {exc}"


def _close_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _read_entries_checked_locked(
    registry_path: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read and validate registry entries, distinguishing empty from I/O/format errors."""
    try:
        fd = os.open(
            registry_path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return [], None
        return [], f"could not open child registry: {exc}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return [], "child registry is not a regular file"
        if st.st_size > _MAX_REGISTRY_BYTES:
            return [], "child registry exceeds maximum size"
        chunks: list[bytes] = []
        remaining = _MAX_REGISTRY_BYTES
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks).decode("utf-8", errors="replace")
    except OSError as exc:
        return [], f"could not read child registry: {exc}"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], f"child registry contains invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return [], "child registry root is not an object"
    raw_entries = payload.get("children")
    if not isinstance(raw_entries, list):
        return [], "child registry missing children list"

    valid: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        owner_pid = item.get("owner_pid")
        owner_ticks = item.get("owner_start_ticks")
        child_pid = item.get("child_pid")
        child_ticks = item.get("child_start_ticks")
        child_pgid = item.get("child_pgid")
        own_group = item.get("own_group")
        boot_id = item.get("boot_id")
        group_token = item.get("group_token")
        raw_descendants = item.get("descendants")
        if (
            not _is_pos_pid(owner_pid)
            or not _is_nonneg_int(owner_ticks)
            or not _is_pos_pid(child_pid)
            or not _is_nonneg_int(child_ticks)
            or not isinstance(own_group, bool)
        ):
            continue
        if child_pgid is not None and not _is_pos_pid(child_pgid):
            continue
        if not isinstance(boot_id, str) or not boot_id.strip():
            continue
        if group_token is not None and not _is_valid_group_token(group_token):
            group_token = None
        descendants: list[list[int]] = []
        if isinstance(raw_descendants, list):
            for d_pair in raw_descendants:
                if (
                    isinstance(d_pair, list)
                    and len(d_pair) == 2
                    and _is_pos_pid(d_pair[0])
                    and _is_nonneg_int(d_pair[1])
                ):
                    descendants.append([d_pair[0], d_pair[1]])
        valid.append(
            {
                "boot_id": boot_id.strip(),
                "owner_pid": owner_pid,
                "owner_start_ticks": owner_ticks,
                "child_pid": child_pid,
                "child_pgid": child_pgid,
                "child_start_ticks": child_ticks,
                "own_group": own_group,
                "provider": str(item.get("provider") or "")[:32],
                "group_token": group_token,
                "descendants": descendants,
            }
        )
    return valid, None


def _write_entries_locked(
    registry_path: Path, entries: list[dict[str, Any]]
) -> str | None:
    payload = json.dumps({"version": 2, "children": entries}, separators=(",", ":"))
    data = payload.encode("utf-8")
    tmp_path: str | None = None
    try:
        try:
            existing = os.lstat(registry_path)
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                return "child registry is not a regular file"
        except FileNotFoundError:
            pass
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{registry_path.name}.",
            suffix=".tmp",
            dir=str(registry_path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, registry_path)
        tmp_path = None
        return None
    except OSError as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return f"could not write child registry: {exc}"


def _entry_descendants(item: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    raw = item.get("descendants")
    if not isinstance(raw, list):
        return ()
    return _normalize_descendants([(pair[0], pair[1]) for pair in raw if len(pair) == 2])


def register_child(
    pid: int,
    *,
    pgid: int | None = None,
    own_group: bool = False,
    provider: str = "",
    group_token: str | None = None,
    registry_path: Path | None = None,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
    require_live: bool = False,
) -> ProcessIdentity | None:
    """Durably record a newly spawned direct child so crashes cannot strand it.

    Returns `None` without writing when `pid` is not a real direct child of this
    process and `require_live` is False (e.g. mock-only tests or non-Linux).
    Raises `ChildRegistryPersistenceError` if a real child exited early leaving
    token-verified descendants, if `require_live` is True on Linux and live
    identity cannot be captured, or if durable registry persistence fails.
    """
    token = group_token if _is_valid_group_token(group_token) else None
    child = capture_child_identity(pid, pgid=pgid, own_group=own_group)
    if child is None or child.state == "Z":
        if token is not None:
            early_desc = _snapshot_token_descendants(token)
            if early_desc:
                raise ChildRegistryPersistenceError(
                    "child exited before registration while descendants remained live",
                    descendants=early_desc,
                )
        if require_live and os.name == "posix" and Path("/proc/self/stat").exists():
            raise ChildRegistryPersistenceError(
                "could not capture live child process identity before registration"
            )
        return None

    recorded_pgid: int | None = None
    recorded_own_group = False
    if own_group:
        candidate = pgid if _is_pos_pid(pgid) else child.pgid
        if _is_pos_pid(candidate) and candidate == child.pid and candidate != _current_pgrp():
            recorded_pgid = candidate
            recorded_own_group = True

    def _capture_descendants() -> tuple[tuple[int, int], ...]:
        group_desc = (
            snapshot_group_descendants(recorded_pgid, child.pid, child.start_ticks)
            if recorded_own_group
            else ()
        )
        token_desc = (
            _snapshot_token_descendants(token, exclude_pid=child.pid)
            if token is not None
            else ()
        )
        return _normalize_descendants((*group_desc, *token_desc))

    descendants = _capture_descendants()

    def _failure_descendants() -> tuple[tuple[int, int], ...]:
        return _normalize_descendants((*descendants, *_capture_descendants()))

    owner = read_process_identity(os.getpid())
    boot_id = current_boot_id()
    if owner is None or boot_id is None:
        raise ChildRegistryPersistenceError(
            "could not read Helios process or kernel boot identity",
            child_identity=child,
            descendants=_failure_descendants(),
        )

    stored_desc, desc_overflow = _prune_live_descendants_for_storage(descendants)

    entry = {
        "boot_id": boot_id,
        "owner_pid": owner.pid,
        "owner_start_ticks": owner.start_ticks,
        "child_pid": child.pid,
        "child_pgid": recorded_pgid,
        "child_start_ticks": child.start_ticks,
        "own_group": recorded_own_group,
        "provider": str(provider or "")[:32],
        "group_token": token,
        "descendants": stored_desc,
    }

    path = _resolve_registry_path(registry_path)
    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    if not _acquire_inproc_lock(deadline):
        raise ChildRegistryPersistenceError(
            "timed out waiting for child registry lock",
            child_identity=child,
            descendants=_failure_descendants(),
        )
    try:
        lock_fd, lock_err = _open_lock(path, deadline=deadline)
        if lock_fd is None:
            raise ChildRegistryPersistenceError(
                lock_err or "could not lock child registry",
                child_identity=child,
                descendants=_failure_descendants(),
            )
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None:
                raise ChildRegistryPersistenceError(
                    read_err,
                    child_identity=child,
                    descendants=_failure_descendants(),
                )
            filtered = [
                item
                for item in entries
                if not (
                    item["owner_pid"] == owner.pid
                    and item["owner_start_ticks"] == owner.start_ticks
                    and item["child_pid"] == child.pid
                    and item["child_start_ticks"] == child.start_ticks
                )
            ]
            filtered.append(entry)
            write_err = _write_entries_locked(path, filtered)
            if write_err is not None:
                raise ChildRegistryPersistenceError(
                    write_err,
                    child_identity=child,
                    descendants=_failure_descendants(),
                )
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()
    if desc_overflow:
        _latch_unpersisted_targets(
            [
                ProcessTreeTarget(
                    pid=child.pid,
                    pgid=recorded_pgid,
                    start_ticks=child.start_ticks,
                    provider=str(provider or "")[:32],
                    owner_pid=owner.pid,
                    registry_path=path,
                    group_token=token,
                    descendants=desc_overflow,
                )
            ]
        )
    return child


def record_child_descendants(
    pid: int,
    *,
    pgid: int | None = None,
    start_ticks: int | None = None,
    registry_path: Path | None = None,
    lock_timeout: float = 0.5,
) -> tuple[tuple[int, int], ...]:
    """Snapshot verified live descendants into the registry while leader is live."""
    if not _is_pos_pid(pid) or not _is_pos_pid(pgid) or not _is_nonneg_int(start_ticks):
        return ()
    captured = snapshot_group_descendants(pgid, pid, start_ticks)
    if not captured:
        return ()
    owner = read_process_identity(os.getpid())
    if owner is None:
        return captured
    path = _resolve_registry_path(registry_path)
    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    if not _acquire_inproc_lock(deadline):
        return captured
    try:
        lock_fd, _ = _open_lock(path, deadline=deadline)
        if lock_fd is None:
            return captured
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None:
                return captured
            updated = False
            for item in entries:
                if (
                    item["owner_pid"] == owner.pid
                    and item["owner_start_ticks"] == owner.start_ticks
                    and item["child_pid"] == pid
                    and item["child_start_ticks"] == start_ticks
                ):
                    merged = _normalize_descendants(
                        (*_entry_descendants(item), *captured)
                    )
                    new_list, overflow = _prune_live_descendants_for_storage(merged)
                    if overflow:
                        _latch_unpersisted_targets(
                            [
                                ProcessTreeTarget(
                                    pid=pid,
                                    pgid=pgid,
                                    start_ticks=start_ticks,
                                    provider=item.get("provider", ""),
                                    owner_pid=owner.pid,
                                    registry_path=path,
                                    group_token=item.get("group_token"),
                                    descendants=overflow,
                                )
                            ]
                        )
                    if item.get("descendants") != new_list:
                        item["descendants"] = new_list
                        updated = True
                    captured = merged
            if updated:
                write_err = _write_entries_locked(path, entries)
                if write_err is not None and captured:
                    _latch_unpersisted_targets(
                        [
                            ProcessTreeTarget(
                                pid=pid,
                                pgid=pgid,
                                start_ticks=start_ticks,
                                owner_pid=owner.pid,
                                registry_path=path,
                                descendants=captured,
                            )
                        ]
                    )
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()
    return captured


def unregister_child(
    pid: int,
    *,
    start_ticks: int | None = None,
    registry_path: Path | None = None,
    lock_timeout: float = 0.5,
) -> None:
    """Remove a child owned by this Helios process from the registry."""
    if not _is_pos_pid(pid):
        return
    owner = read_process_identity(os.getpid())
    if owner is None:
        return
    path = _resolve_registry_path(registry_path)
    if not path.exists():
        return
    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    if not _acquire_inproc_lock(deadline):
        return
    try:
        lock_fd, _ = _open_lock(path, deadline=deadline)
        if lock_fd is None:
            return
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None or not entries:
                return
            kept = [
                item
                for item in entries
                if not (
                    item["owner_pid"] == owner.pid
                    and item["owner_start_ticks"] == owner.start_ticks
                    and item["child_pid"] == pid
                    and (
                        start_ticks is None
                        or item["child_start_ticks"] == start_ticks
                    )
                )
            ]
            if len(kept) != len(entries):
                _write_entries_locked(path, kept)
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()


def unregister_if_dead(
    pid: int,
    *,
    pgid: int | None = None,
    start_ticks: int | None = None,
    group_token: str | None = None,
    descendants: Sequence[tuple[int, int]] = (),
    registry_path: Path | None = None,
    lock_timeout: float = 0.5,
) -> bool:
    """Unregister a child only when its full tree (including stored descendants) is dead.

    Loads and merges durably recorded descendants under the registry lock before
    deciding whether to remove the entry, and persists any newly discovered
    descendants when the entry must be retained.
    """
    if not _is_pos_pid(pid):
        return True
    owner = read_process_identity(os.getpid())
    path = _resolve_registry_path(registry_path)
    if owner is None or not path.exists():
        status = classify_process_tree(
            pgid,
            pid,
            expected_start_ticks=start_ticks,
            expected_descendants=descendants,
            expected_group_token=group_token,
        )
        return status in ("dead", "recycled")

    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    if not _acquire_inproc_lock(deadline):
        return False
    try:
        lock_fd, _ = _open_lock(path, deadline=deadline)
        if lock_fd is None:
            return False
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None:
                return False
            matched_any = False
            any_kept_live = False
            retained_refreshed: list[ProcessTreeTarget] = []
            kept: list[dict[str, Any]] = []
            changed = False
            for item in entries:
                is_match = (
                    item["owner_pid"] == owner.pid
                    and item["owner_start_ticks"] == owner.start_ticks
                    and item["child_pid"] == pid
                    and (
                        start_ticks is None
                        or item["child_start_ticks"] == start_ticks
                    )
                )
                if not is_match:
                    kept.append(item)
                    continue
                matched_any = True
                merged_desc = _normalize_descendants(
                    (*descendants, *_entry_descendants(item))
                )
                merged_token = group_token or item.get("group_token")
                merged_pgid = (
                    pgid
                    if pgid is not None
                    else (item["child_pgid"] if item["own_group"] else None)
                )
                merged_ticks = (
                    start_ticks
                    if start_ticks is not None
                    else item["child_start_ticks"]
                )
                target = ProcessTreeTarget(
                    pid=pid,
                    pgid=merged_pgid,
                    start_ticks=merged_ticks,
                    group_token=merged_token,
                    descendants=merged_desc,
                    registry_path=path,
                )
                status, refreshed = inspect_process_tree(target)
                if status in ("dead", "recycled"):
                    changed = True
                    continue
                any_kept_live = True
                retained_refreshed.append(refreshed)
                if refreshed.descendants:
                    new_list, overflow = _prune_live_descendants_for_storage(
                        refreshed.descendants
                    )
                    if overflow:
                        _latch_unpersisted_targets(
                            [replace(refreshed, descendants=overflow)]
                        )
                    if item.get("descendants") != new_list:
                        item = dict(item)
                        item["descendants"] = new_list
                        changed = True
                kept.append(item)

            if changed:
                write_err = _write_entries_locked(path, kept)
                if write_err is not None and retained_refreshed:
                    _latch_unpersisted_targets(retained_refreshed)
            if matched_any:
                return not any_kept_live
            status = classify_process_tree(
                pgid,
                pid,
                expected_start_ticks=start_ticks,
                expected_descendants=descendants,
                expected_group_token=group_token,
            )
            return status in ("dead", "recycled")
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()


def owned_child_targets(
    *,
    registry_path: Path | None = None,
    lock_timeout: float = 0.5,
) -> list[ProcessTreeTarget]:
    """Return all registry targets currently owned by this Helios process."""
    owner = read_process_identity(os.getpid())
    boot_id = current_boot_id()
    if owner is None or boot_id is None:
        return []
    path = _resolve_registry_path(registry_path)
    try:
        st = os.lstat(path)
    except OSError:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return []
    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    if not _acquire_inproc_lock(deadline):
        return []
    try:
        lock_fd, _ = _open_lock(path, deadline=deadline)
        if lock_fd is None:
            return []
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None or not entries:
                return []
            my_pgid = _current_pgrp()
            targets: list[ProcessTreeTarget] = []
            for item in entries:
                if item.get("boot_id") != boot_id:
                    continue
                if (
                    item["owner_pid"] != owner.pid
                    or item["owner_start_ticks"] != owner.start_ticks
                ):
                    continue
                child_pid = item["child_pid"]
                if child_pid == owner.pid:
                    continue
                child_pgid = item["child_pgid"]
                safe_pgid = (
                    child_pgid
                    if (
                        item["own_group"]
                        and _is_pos_pid(child_pgid)
                        and child_pgid == child_pid
                        and child_pgid != my_pgid
                    )
                    else None
                )
                targets.append(
                    ProcessTreeTarget(
                        pid=child_pid,
                        pgid=safe_pgid,
                        start_ticks=item["child_start_ticks"],
                        provider=item["provider"],
                        owner_pid=owner.pid,
                        registry_path=path,
                        group_token=item.get("group_token"),
                        descendants=_entry_descendants(item),
                    )
                )
            return targets
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()


def _poll_quietly(target: ProcessTreeTarget) -> None:
    if target.poll is None:
        return
    try:
        target.poll()
    except Exception:
        pass


def _refresh_target_descendants(target: ProcessTreeTarget) -> ProcessTreeTarget:
    my_uid = _current_uid()
    my_pgid = _current_pgrp()
    extra: list[tuple[int, int]] = []
    group_leaders: list[tuple[int, int]] = []
    seen_leaders: set[tuple[int, int]] = set()

    if target.pgid is not None and target.start_ticks is not None:
        extra.extend(
            snapshot_group_descendants(target.pgid, target.pid, target.start_ticks)
        )
        seen_leaders.add((target.pid, target.start_ticks))

    # Include any already-tracked verified descendants that lead their own group.
    for d_pid, d_ticks in _normalize_descendants(target.descendants):
        d_ident = read_process_identity(d_pid)
        if (
            d_ident is not None
            and d_ident.uid == my_uid
            and d_ident.start_ticks == d_ticks
            and d_ident.pgid == d_ident.pid
            and d_ident.pgid != my_pgid
            and (d_pid, d_ticks) not in seen_leaders
        ):
            group_leaders.append((d_pid, d_ticks))
            seen_leaders.add((d_pid, d_ticks))

    # Discover token-bearing members AND snapshot every member of their groups too
    # (so token-scrubbed children inside an escaped session group are captured before
    # their escaped session leader exits).
    if _is_valid_group_token(target.group_token):
        for member in scan_token_members(target.group_token):
            if member.pid != target.pid:
                extra.append((member.pid, member.start_ticks))
            if (
                member.pgid == member.pid
                and _is_pos_pid(member.pgid)
                and member.pgid != my_pgid
                and (member.pid, member.start_ticks) not in seen_leaders
            ):
                group_leaders.append((member.pid, member.start_ticks))
                seen_leaders.add((member.pid, member.start_ticks))

    for g_pid, g_ticks in group_leaders:
        for m_pid, m_ticks in snapshot_group_descendants(g_pid, g_pid, g_ticks):
            if m_pid != target.pid:
                extra.append((m_pid, m_ticks))

    if not extra:
        return target
    merged = _normalize_descendants((*target.descendants, *extra))
    if merged == target.descendants:
        return target
    return replace(target, descendants=merged)


def _target_key(target: ProcessTreeTarget) -> tuple[int | None, int, int | None]:
    return (target.pgid, target.pid, target.start_ticks)


def _latch_unpersisted_targets(targets: Sequence[ProcessTreeTarget]) -> None:
    if not targets:
        return
    with _ABORT_LATCH_LOCK:
        for item in targets:
            _upsert_abort_latch_locked(item)


def _batch_update_registry(
    removals: Sequence[ProcessTreeTarget],
    updates: Sequence[ProcessTreeTarget],
    *,
    deadline: float,
) -> tuple[list[ProcessTreeTarget], list[ProcessTreeTarget]]:
    """Revalidate & remove completed targets and persist discovered descendants in one lock.

    Before deleting any matching row, merges durably recorded descendants from
    that row and revalidates liveness under the lock. Rows whose stored
    descendants are still live or unresolved are retained and returned as
    `(survived_live, survived_unresolved)` instead of being deleted.
    """
    survived_live: list[ProcessTreeTarget] = []
    survived_unresolved: list[ProcessTreeTarget] = []
    if not removals and not updates:
        return survived_live, survived_unresolved
    owner = read_process_identity(os.getpid())
    if owner is None:
        _latch_unpersisted_targets([t for t in updates if t.descendants])
        return survived_live, survived_unresolved

    removals_by_path: dict[Path, list[ProcessTreeTarget]] = {}
    updates_by_path: dict[Path, list[ProcessTreeTarget]] = {}
    all_paths: list[Path] = []
    for target in removals:
        if not _is_pos_pid(target.pid):
            continue
        path = _resolve_registry_path(target.registry_path)
        if path not in removals_by_path and path not in updates_by_path:
            all_paths.append(path)
        removals_by_path.setdefault(path, []).append(target)
    for target in updates:
        if not _is_pos_pid(target.pid) or not target.descendants:
            continue
        path = _resolve_registry_path(target.registry_path)
        if path not in removals_by_path and path not in updates_by_path:
            all_paths.append(path)
        updates_by_path.setdefault(path, []).append(target)

    if not all_paths:
        return survived_live, survived_unresolved

    for idx, path in enumerate(all_paths):
        path_updates = updates_by_path.get(path, [])
        path_removals = removals_by_path.get(path, [])
        if not path.exists():
            continue
        # Even when deadline has elapsed, attempt one non-blocking lock so an
        # uncontended registry still clears dead rows without adding any wait.
        if not _acquire_inproc_lock(deadline):
            for rest_path in all_paths[idx:]:
                _latch_unpersisted_targets(updates_by_path.get(rest_path, []))
            return survived_live, survived_unresolved
        try:
            lock_fd, _ = _open_lock(path, deadline=deadline)
            if lock_fd is None:
                _latch_unpersisted_targets(path_updates)
                continue
            try:
                entries, read_err = _read_entries_checked_locked(path)
                if read_err is not None:
                    _latch_unpersisted_targets(path_updates)
                    continue
                if not entries:
                    continue
                kept: list[dict[str, Any]] = []
                changed = False
                retained_for_latch: list[ProcessTreeTarget] = list(path_updates)
                for item in entries:
                    if (
                        item["owner_pid"] == owner.pid
                        and item["owner_start_ticks"] == owner.start_ticks
                    ):
                        matched_removal = next(
                            (
                                r_target
                                for r_target in path_removals
                                if item["child_pid"] == r_target.pid
                                and (
                                    r_target.start_ticks is None
                                    or item["child_start_ticks"] == r_target.start_ticks
                                )
                            ),
                            None,
                        )
                        if matched_removal is not None:
                            stored_desc = _entry_descendants(item)
                            merged_desc = _normalize_descendants(
                                (*matched_removal.descendants, *stored_desc)
                            )
                            merged_token = matched_removal.group_token or item.get("group_token")
                            merged_pgid = (
                                matched_removal.pgid
                                if matched_removal.pgid is not None
                                else (item["child_pgid"] if item["own_group"] else None)
                            )
                            merged_ticks = (
                                matched_removal.start_ticks
                                if matched_removal.start_ticks is not None
                                else item["child_start_ticks"]
                            )
                            recheck = replace(
                                matched_removal,
                                pgid=merged_pgid,
                                start_ticks=merged_ticks,
                                group_token=merged_token,
                                descendants=merged_desc,
                                registry_path=path,
                            )
                            status, refreshed = inspect_process_tree(recheck)
                            if status in ("dead", "recycled"):
                                changed = True
                                continue
                            # Stored descendants are still live or unresolved: do NOT delete row!
                            if status == "live":
                                survived_live.append(refreshed)
                            else:
                                survived_unresolved.append(refreshed)
                            retained_for_latch.append(refreshed)
                            if refreshed.descendants:
                                new_list, overflow = _prune_live_descendants_for_storage(
                                    refreshed.descendants
                                )
                                if overflow:
                                    _latch_unpersisted_targets(
                                        [replace(refreshed, descendants=overflow)]
                                    )
                                if item.get("descendants") != new_list:
                                    item = dict(item)
                                    item["descendants"] = new_list
                                    changed = True
                            kept.append(item)
                            continue
                        for upd in path_updates:
                            if item["child_pid"] == upd.pid and (
                                upd.start_ticks is None
                                or item["child_start_ticks"] == upd.start_ticks
                            ):
                                merged = _normalize_descendants(
                                    (*_entry_descendants(item), *upd.descendants)
                                )
                                new_list, overflow = _prune_live_descendants_for_storage(
                                    merged
                                )
                                if overflow:
                                    _latch_unpersisted_targets(
                                        [replace(upd, descendants=overflow)]
                                    )
                                if item.get("descendants") != new_list:
                                    item = dict(item)
                                    item["descendants"] = new_list
                                    changed = True
                    kept.append(item)
                if changed:
                    write_err = _write_entries_locked(path, kept)
                    if write_err is not None:
                        _latch_unpersisted_targets(retained_for_latch)
            finally:
                _close_lock(lock_fd)
        finally:
            _INPROC_LOCK.release()
    return survived_live, survived_unresolved


def reap_process_trees(
    targets: Sequence[ProcessTreeTarget],
    *,
    timeout: float = 1.5,
    kill_wait: float = 0.25,
    send_term: bool = True,
    unregister: bool = True,
    _reap_stored_survivors: bool = True,
) -> ReapResult:
    """Reap multiple child process trees in parallel within one shared budget.

    Establishes the shared deadline at entry, signals live targets before any
    registry maintenance, never signals or unregisters `"unresolved"` groups,
    and batches all registry unregisters inside the remaining budget.
    """
    started = time.monotonic()
    term_budget = max(0.0, float(timeout))
    kill_budget = max(0.0, float(kill_wait))
    term_deadline = started + term_budget
    total_deadline = started + term_budget + kill_budget

    merged_by_key: dict[tuple[int | None, int, int | None], ProcessTreeTarget] = {}
    order: list[tuple[int | None, int, int | None]] = []
    for target in targets:
        if not _is_pos_pid(target.pid):
            continue
        key = _target_key(target)
        existing = merged_by_key.get(key)
        if existing is None:
            merged_by_key[key] = target
            order.append(key)
        else:
            merged_by_key[key] = replace(
                existing,
                poll=existing.poll or target.poll,
                provider=existing.provider or target.provider,
                owner_pid=existing.owner_pid or target.owner_pid,
                registry_path=existing.registry_path or target.registry_path,
                group_token=existing.group_token or target.group_token,
                descendants=_normalize_descendants(
                    (*existing.descendants, *target.descendants)
                ),
            )

    unique = [_refresh_target_descendants(merged_by_key[k]) for k in order]
    if not unique:
        return ReapResult(reaped=(), remaining=(), unresolved=())

    for target in unique:
        _poll_quietly(target)

    live: list[ProcessTreeTarget] = []
    unresolved: list[ProcessTreeTarget] = []
    already_dead: list[ProcessTreeTarget] = []
    for target in unique:
        status, target = inspect_process_tree(target)
        if status == "live":
            live.append(target)
        elif status == "unresolved":
            unresolved.append(target)
        else:
            already_dead.append(target)

    if not live:
        if unregister and (already_dead or unresolved):
            stored_live, stored_unres = _batch_update_registry(
                already_dead, unresolved, deadline=total_deadline
            )
            unresolved.extend(stored_unres)
            if stored_live:
                if _reap_stored_survivors:
                    rem_timeout = max(0.0, term_deadline - time.monotonic())
                    sub = reap_process_trees(
                        stored_live,
                        timeout=rem_timeout,
                        kill_wait=kill_budget,
                        send_term=True,
                        unregister=True,
                        _reap_stored_survivors=False,
                    )
                    return ReapResult(
                        reaped=sub.reaped,
                        remaining=sub.remaining,
                        unresolved=tuple([*unresolved, *sub.unresolved]),
                    )
                return ReapResult(
                    reaped=(),
                    remaining=tuple(stored_live),
                    unresolved=tuple(unresolved),
                )
        return ReapResult(reaped=(), remaining=(), unresolved=tuple(unresolved))

    # Signal live targets FIRST before any registry lock or I/O, merging any
    # newly discovered escaped groups/members into the retained targets.
    if send_term:
        for idx, target in enumerate(live):
            discovered: list[tuple[int, int]] = []
            signal_process_tree(
                target.pgid,
                target.pid,
                signal.SIGTERM,
                wake_stopped=True,
                expected_start_ticks=target.start_ticks,
                expected_descendants=target.descendants,
                expected_group_token=target.group_token,
                discovered_out=discovered,
            )
            if discovered:
                merged = _normalize_descendants((*target.descendants, *discovered))
                if merged != target.descendants:
                    live[idx] = replace(target, descendants=merged)

    still_live = list(live)
    while still_live:
        next_live: list[ProcessTreeTarget] = []
        for target in still_live:
            _poll_quietly(target)
            status, target = inspect_process_tree(target)
            if status == "live":
                next_live.append(target)
            elif status == "unresolved":
                unresolved.append(target)
        still_live = next_live
        if not still_live:
            break
        remaining_s = term_deadline - time.monotonic()
        if remaining_s <= 0:
            break
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining_s))

    if still_live:
        for idx, target in enumerate(still_live):
            discovered = []
            signal_process_tree(
                target.pgid,
                target.pid,
                signal.SIGKILL,
                wake_stopped=False,
                expected_start_ticks=target.start_ticks,
                expected_descendants=target.descendants,
                expected_group_token=target.group_token,
                discovered_out=discovered,
            )
            if discovered:
                merged = _normalize_descendants((*target.descendants, *discovered))
                if merged != target.descendants:
                    still_live[idx] = replace(target, descendants=merged)
        kill_deadline = min(time.monotonic() + kill_budget, total_deadline)
        while still_live:
            next_live = []
            for target in still_live:
                _poll_quietly(target)
                status, target = inspect_process_tree(target)
                if status == "live":
                    next_live.append(target)
                elif status == "unresolved":
                    unresolved.append(target)
            still_live = next_live
            if not still_live:
                break
            remaining_s = kill_deadline - time.monotonic()
            if remaining_s <= 0:
                break
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining_s))

    remaining_keys = {_target_key(t) for t in still_live}
    unresolved_keys = {_target_key(t) for t in unresolved}
    reaped = [
        t
        for t in live
        if _target_key(t) not in remaining_keys and _target_key(t) not in unresolved_keys
    ]
    if unregister:
        to_remove = [*already_dead, *reaped]
        to_update = [*still_live, *unresolved]
        if to_remove or to_update:
            update_deadline = (
                max(total_deadline, time.monotonic() + 0.15)
                if any(t.descendants for t in to_update)
                else total_deadline
            )
            stored_live, stored_unres = _batch_update_registry(
                to_remove, to_update, deadline=update_deadline
            )
            if stored_unres:
                unresolved.extend(stored_unres)
            if stored_live:
                if _reap_stored_survivors:
                    rem_timeout = max(0.0, term_deadline - time.monotonic())
                    sub = reap_process_trees(
                        stored_live,
                        timeout=rem_timeout,
                        kill_wait=kill_budget,
                        send_term=True,
                        unregister=True,
                        _reap_stored_survivors=False,
                    )
                    still_live.extend(sub.remaining)
                    unresolved.extend(sub.unresolved)
                    sub_bad = {_target_key(t) for t in (*sub.remaining, *sub.unresolved)}
                    reaped = [t for t in reaped if _target_key(t) not in sub_bad]
                else:
                    still_live.extend(stored_live)
                    bad_keys = {_target_key(t) for t in stored_live}
                    reaped = [t for t in reaped if _target_key(t) not in bad_keys]
            if stored_unres:
                unres_keys = {_target_key(t) for t in stored_unres}
                reaped = [t for t in reaped if _target_key(t) not in unres_keys]
    return ReapResult(
        reaped=tuple(reaped),
        remaining=tuple(still_live),
        unresolved=tuple(unresolved),
    )


def _summary_dict(target: ProcessTreeTarget) -> dict[str, Any]:
    return {
        "pid": target.pid,
        "pgid": target.pgid,
        "provider": target.provider,
        "owner_pid": target.owner_pid,
    }


def _reap_registry_entries(
    *,
    registry_path: Path | None,
    grace_seconds: float,
    lock_timeout: float | None,
    scope: Literal["orphans", "owned"],
) -> OrphanReapOutcome:
    path = _resolve_registry_path(registry_path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return OrphanReapOutcome()
    except OSError as exc:
        return OrphanReapOutcome(error=f"could not inspect child registry: {exc}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return OrphanReapOutcome(error="child registry is not a regular file")

    current = read_process_identity(os.getpid())
    boot_id = current_boot_id()
    if current is None or boot_id is None:
        return OrphanReapOutcome(error="process identity unavailable")

    started = time.monotonic()
    grace = max(0.0, float(grace_seconds))
    lock_budget = (
        max(0.05, grace) if lock_timeout is None else max(0.0, float(lock_timeout))
    )
    lock_deadline = started + lock_budget

    if not _acquire_inproc_lock(lock_deadline):
        return OrphanReapOutcome(error="timed out waiting for child registry lock")
    try:
        lock_fd, lock_err = _open_lock(path, deadline=lock_deadline)
        if lock_fd is None:
            return OrphanReapOutcome(error=lock_err or "could not lock child registry")
        try:
            entries, read_err = _read_entries_checked_locked(path)
            if read_err is not None:
                return OrphanReapOutcome(error=read_err)
            if not entries:
                return OrphanReapOutcome()

            kept: list[dict[str, Any]] = []
            reap_targets: list[tuple[dict[str, Any], ProcessTreeTarget]] = []
            unresolved_targets: list[ProcessTreeTarget] = []
            unresolved_summaries: list[dict[str, Any]] = []
            my_pgid = _current_pgrp()
            my_uid = _current_uid()

            for item in entries:
                if item.get("boot_id") != boot_id:
                    # Machine rebooted since this entry was recorded; drop stale row.
                    continue

                owner_pid = item["owner_pid"]
                owner_ticks = item["owner_start_ticks"]
                is_mine = (
                    owner_pid == current.pid and owner_ticks == current.start_ticks
                )
                if scope == "orphans":
                    if is_mine or not _owner_is_dead(owner_pid, owner_ticks):
                        kept.append(item)
                        continue
                else:
                    if not is_mine:
                        kept.append(item)
                        continue

                child_pid = item["child_pid"]
                child_ticks = item["child_start_ticks"]
                own_group = item["own_group"]
                child_pgid = item["child_pgid"]
                if child_pid == current.pid:
                    continue

                safe_pgid = (
                    child_pgid
                    if (
                        own_group
                        and _is_pos_pid(child_pgid)
                        and child_pgid == child_pid
                        and child_pgid != my_pgid
                    )
                    else None
                )
                descendants = _entry_descendants(item)
                group_token = item.get("group_token")

                child_ident = read_process_identity(child_pid)
                if (
                    child_ident is not None
                    and child_ident.uid == my_uid
                    and child_ident.start_ticks == child_ticks
                    and child_ident.state != "Z"
                    and safe_pgid is not None
                    and child_ident.pgid != safe_pgid
                ):
                    safe_pgid = None

                target = ProcessTreeTarget(
                    pid=child_pid,
                    pgid=safe_pgid,
                    start_ticks=child_ticks,
                    provider=item["provider"],
                    owner_pid=owner_pid,
                    registry_path=path,
                    group_token=group_token,
                    descendants=descendants,
                )
                status, target = inspect_process_tree(target)
                if status == "live":
                    reap_targets.append((item, target))
                elif status == "unresolved":
                    unresolved_targets.append(target)
                    if target.descendants:
                        new_list, overflow = _prune_live_descendants_for_storage(
                            target.descendants
                        )
                        if overflow:
                            _latch_unpersisted_targets(
                                [replace(target, descendants=overflow)]
                            )
                        item = dict(item)
                        item["descendants"] = new_list
                    kept.append(item)
                    unresolved_summaries.append(_summary_dict(target))

            if not reap_targets:
                write_err = (
                    _write_entries_locked(path, kept)
                    if kept != entries
                    else None
                )
                if write_err is not None and unresolved_targets:
                    _latch_unpersisted_targets(unresolved_targets)
                return OrphanReapOutcome(
                    unresolved=tuple(unresolved_summaries),
                    error=write_err,
                )

            reap_budget = max(0.0, (started + grace) - time.monotonic())
            result = reap_process_trees(
                [target for _, target in reap_targets],
                timeout=reap_budget,
                send_term=True,
                unregister=False,
            )
            remaining_by_key = {_target_key(t): t for t in result.remaining}
            unresolved_by_key = {_target_key(t): t for t in result.unresolved}
            for item, target in reap_targets:
                key = _target_key(target)
                updated_target = remaining_by_key.get(key) or unresolved_by_key.get(key)
                if updated_target is not None:
                    if updated_target.descendants:
                        new_list, overflow = _prune_live_descendants_for_storage(
                            updated_target.descendants
                        )
                        if overflow:
                            _latch_unpersisted_targets(
                                [replace(updated_target, descendants=overflow)]
                            )
                        item = dict(item)
                        item["descendants"] = new_list
                    kept.append(item)

            for target in result.unresolved:
                unresolved_summaries.append(_summary_dict(target))

            write_err = _write_entries_locked(path, kept)
            if write_err is not None:
                _latch_unpersisted_targets(
                    [*unresolved_targets, *result.remaining, *result.unresolved]
                )
            return OrphanReapOutcome(
                reaped=tuple(_summary_dict(t) for t in result.reaped),
                remaining=tuple(_summary_dict(t) for t in result.remaining),
                unresolved=tuple(unresolved_summaries),
                error=write_err,
            )
        finally:
            _close_lock(lock_fd)
    finally:
        _INPROC_LOCK.release()


def reap_orphaned_children(
    *,
    registry_path: Path | None = None,
    grace_seconds: float = 1.0,
    lock_timeout: float | None = None,
) -> OrphanReapOutcome:
    """Reap verified child process groups left behind by a dead Helios."""
    return _reap_registry_entries(
        registry_path=registry_path,
        grace_seconds=grace_seconds,
        lock_timeout=lock_timeout,
        scope="orphans",
    )


def reap_owned_children(
    *,
    registry_path: Path | None = None,
    grace_seconds: float = 1.0,
    lock_timeout: float | None = None,
) -> OrphanReapOutcome:
    """Reap verified child process groups owned by this Helios process."""
    return _reap_registry_entries(
        registry_path=registry_path,
        grace_seconds=grace_seconds,
        lock_timeout=lock_timeout,
        scope="owned",
    )
