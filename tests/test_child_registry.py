"""Real-process and identity-safety tests for agent child-process reaping."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helios.backend.process import child_registry
from helios.backend.process.child_registry import (
    CHILD_GROUP_ENV,
    ChildRegistryPersistenceError,
    ProcessTreeTarget,
    owned_child_targets,
    wait_for_process_group,
    classify_process_tree,
    current_boot_id,
    is_process_tree_live,
    new_group_token,
    read_process_identity,
    reap_orphaned_children,
    reap_process_trees,
    register_child,
    signal_process_tree,
    unregister_child,
    unregister_if_dead,
    wake_if_stopped,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/proc/self/stat").exists(),
    reason="Linux /proc process identity required",
)


def _spawn_sleeper(
    *,
    ignore_term: bool = False,
    with_grandchild: bool = False,
    start_new_session: bool = True,
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Spawn a real Python sleeper (optionally ignoring SIGTERM and/or spawning a child)."""
    code = [
        "import os, signal, subprocess, sys, time",
    ]
    if ignore_term:
        code.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    if with_grandchild:
        code.append(
            "gc = subprocess.Popen([sys.executable, \"-c\", "
            "\"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\"])"
        )
        code.append("print(gc.pid, flush=True)")
    else:
        code.append("print(0, flush=True)")
    code.append("time.sleep(60)")
    proc = subprocess.Popen(
        [sys.executable, "-c", "\n".join(code)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=start_new_session,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
    grandchild_pid = int(line) if line.isdigit() and int(line) > 1 else None
    return proc, grandchild_pid


def _cleanup_proc(proc: subprocess.Popen[bytes], grandchild_pid: int | None = None) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    if grandchild_pid is not None:
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def test_register_ignores_fake_or_non_child_pids(tmp_path):
    reg = tmp_path / "live-children.json"
    assert register_child(1, registry_path=reg) is None
    assert register_child(os.getpid(), registry_path=reg) is None
    assert register_child(9999999, registry_path=reg) is None
    assert not reg.exists()


def test_register_and_unregister_real_child_sets_0600_mode(tmp_path):
    reg = tmp_path / "live-children.json"
    proc, _ = _spawn_sleeper()
    try:
        ident = register_child(
            proc.pid, pgid=proc.pid, own_group=True, provider="anthropic", registry_path=reg
        )
        assert ident is not None
        assert ident.pid == proc.pid
        assert stat.S_IMODE(reg.stat().st_mode) == 0o600
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert len(data["children"]) == 1
        assert data["children"][0]["child_pid"] == proc.pid
        assert data["children"][0]["own_group"] is True

        unregister_child(proc.pid, start_ticks=ident.start_ticks, registry_path=reg)
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert data["children"] == []
    finally:
        _cleanup_proc(proc)


def test_reap_process_trees_wakes_sigstopped_child_and_terminates(tmp_path):
    reg = tmp_path / "live-children.json"
    proc, _ = _spawn_sleeper(ignore_term=False)
    try:
        ident = register_child(
            proc.pid, pgid=proc.pid, own_group=True, provider="anthropic", registry_path=reg
        )
        assert ident is not None
        os.killpg(proc.pid, signal.SIGSTOP)
        # Wait briefly for kernel to mark state T.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            snap = read_process_identity(proc.pid)
            if snap is not None and snap.state == "T":
                break
            time.sleep(0.01)
        assert read_process_identity(proc.pid).state == "T"

        target = ProcessTreeTarget(
            pid=proc.pid,
            pgid=proc.pid,
            start_ticks=ident.start_ticks,
            poll=proc.poll,
            provider="anthropic",
            registry_path=reg,
        )
        result = reap_process_trees([target], timeout=1.0, send_term=True)
        assert len(result.reaped) == 1
        assert result.remaining == ()
        assert not is_process_tree_live(proc.pid, proc.pid, expected_start_ticks=ident.start_ticks)
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        _cleanup_proc(proc)


def test_wake_if_stopped_wakes_real_stopped_child_and_noops_on_running():
    proc, _ = _spawn_sleeper()
    try:
        ident = read_process_identity(proc.pid)
        assert ident is not None
        assert wake_if_stopped(proc.pid, proc.pid, expected_start_ticks=ident.start_ticks) is False
        os.killpg(proc.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            snap = read_process_identity(proc.pid)
            if snap is not None and snap.state == "T":
                break
            time.sleep(0.01)
        assert wake_if_stopped(proc.pid, proc.pid, expected_start_ticks=ident.start_ticks) is True
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            snap = read_process_identity(proc.pid)
            if snap is not None and snap.state != "T":
                break
            time.sleep(0.01)
        assert read_process_identity(proc.pid).state != "T"
    finally:
        _cleanup_proc(proc)


def test_reap_process_trees_sigkills_sigterm_ignoring_leader_and_grandchild(tmp_path):
    reg = tmp_path / "live-children.json"
    proc, grandchild_pid = _spawn_sleeper(ignore_term=True, with_grandchild=True)
    assert grandchild_pid is not None
    try:
        ident = register_child(
            proc.pid, pgid=proc.pid, own_group=True, provider="anthropic", registry_path=reg
        )
        assert ident is not None
        target = ProcessTreeTarget(
            pid=proc.pid,
            pgid=proc.pid,
            start_ticks=ident.start_ticks,
            poll=proc.poll,
            provider="anthropic",
            registry_path=reg,
        )
        started = time.monotonic()
        result = reap_process_trees([target], timeout=0.2, send_term=True)
        elapsed = time.monotonic() - started
        assert elapsed < 1.5
        assert len(result.reaped) == 1
        assert result.remaining == ()
        assert not is_process_tree_live(proc.pid, proc.pid, expected_start_ticks=ident.start_ticks)
        assert read_process_identity(grandchild_pid) is None or read_process_identity(grandchild_pid).state == "Z"
    finally:
        _cleanup_proc(proc, grandchild_pid)


def test_reap_orphaned_children_reaps_dead_owner_group_and_keeps_live_owner(tmp_path):
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None

    dead_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    live_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    live_owner_ident = read_process_identity(live_owner.pid)
    assert live_owner_ident is not None

    orphan_proc, orphan_gc = _spawn_sleeper(ignore_term=True, with_grandchild=True)
    sibling_proc, _ = _spawn_sleeper(ignore_term=False)
    assert orphan_gc is not None
    try:
        orphan_ident = read_process_identity(orphan_proc.pid)
        sibling_ident = read_process_identity(sibling_proc.pid)
        assert orphan_ident is not None and sibling_ident is not None
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": orphan_ident.pid,
                            "child_pgid": orphan_ident.pid,
                            "child_start_ticks": orphan_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                        },
                        {
                            "boot_id": boot_id,
                            "owner_pid": live_owner_ident.pid,
                            "owner_start_ticks": live_owner_ident.start_ticks,
                            "child_pid": sibling_ident.pid,
                            "child_pgid": sibling_ident.pid,
                            "child_start_ticks": sibling_ident.start_ticks,
                            "own_group": True,
                            "provider": "google",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.2)
        assert outcome.ok is True
        assert len(outcome.reaped) == 1
        assert outcome.reaped[0]["pid"] == orphan_proc.pid
        assert outcome.reaped[0]["provider"] == "anthropic"
        orphan_proc.wait(timeout=2)
        assert not is_process_tree_live(
            orphan_proc.pid, orphan_proc.pid, expected_start_ticks=orphan_ident.start_ticks
        )
        assert read_process_identity(orphan_gc) is None or read_process_identity(orphan_gc).state == "Z"

        # Live sibling owner entry and child must remain untouched.
        assert sibling_proc.poll() is None
        remaining = json.loads(reg.read_text(encoding="utf-8"))["children"]
        assert len(remaining) == 1
        assert remaining[0]["child_pid"] == sibling_proc.pid
    finally:
        _cleanup_proc(orphan_proc, orphan_gc)
        _cleanup_proc(sibling_proc)
        _cleanup_proc(live_owner)


def test_reap_orphaned_children_refuses_mismatched_start_ticks(tmp_path):
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    innocent, _ = _spawn_sleeper()
    try:
        innocent_ident = read_process_identity(innocent.pid)
        assert innocent_ident is not None
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": innocent_ident.pid,
                            "child_pgid": innocent_ident.pid,
                            "child_start_ticks": innocent_ident.start_ticks + 99999,
                            "own_group": True,
                            "provider": "anthropic",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.1)
        assert outcome.ok is True
        assert outcome.reaped == ()
        assert innocent.poll() is None
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        _cleanup_proc(innocent)


def test_boot_id_mismatch_drops_stale_entry_without_signalling(tmp_path):
    reg = tmp_path / "live-children.json"
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    innocent, _ = _spawn_sleeper()
    try:
        innocent_ident = read_process_identity(innocent.pid)
        assert innocent_ident is not None
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": "00000000-0000-0000-0000-000000000000",
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": innocent_ident.pid,
                            "child_pgid": innocent_ident.pid,
                            "child_start_ticks": innocent_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.1)
        assert outcome.ok is True
        assert outcome.reaped == ()
        assert innocent.poll() is None
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        _cleanup_proc(innocent)


def test_registry_reports_errors_on_symlinks_and_corrupt_json(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("do not touch", encoding="utf-8")
    link = tmp_path / "live-children.json"
    link.symlink_to(target)

    link_outcome = reap_orphaned_children(registry_path=link, grace_seconds=0.1)
    assert link_outcome.ok is False
    assert link_outcome.error is not None
    assert target.read_text(encoding="utf-8") == "do not touch"

    proc, _ = _spawn_sleeper()
    try:
        with pytest.raises(ChildRegistryPersistenceError):
            register_child(proc.pid, pgid=proc.pid, own_group=True, registry_path=link)
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "do not touch"
    finally:
        _cleanup_proc(proc)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    corrupt_outcome = reap_orphaned_children(registry_path=corrupt, grace_seconds=0.1)
    assert corrupt_outcome.ok is False
    assert corrupt_outcome.error is not None


def _spawn_exiting_leader_with_grandchild(env: dict[str, str] | None = None):
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}])\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    leader.wait(timeout=2)
    return leader, leader_ident, gc_pid, gc_ident


def test_recycled_leaderless_pgid_is_never_signalled_and_preserved(tmp_path):
    """Finding 1: an unverified leaderless PGID must NOT be signalled."""
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    leader, leader_ident, gc_pid, _ = _spawn_exiting_leader_with_grandchild()
    try:
        recorded_earlier_ticks = max(0, leader_ident.start_ticks - 10)
        wrong_token = new_group_token()
        assert (
            classify_process_tree(
                leader_ident.pid,
                leader_ident.pid,
                expected_start_ticks=recorded_earlier_ticks,
                expected_group_token=wrong_token,
            )
            == "unresolved"
        )
        assert not is_process_tree_live(
            leader_ident.pid,
            leader_ident.pid,
            expected_start_ticks=recorded_earlier_ticks,
            expected_group_token=wrong_token,
        )
        assert not signal_process_tree(
            leader_ident.pid,
            leader_ident.pid,
            signal.SIGKILL,
            expected_start_ticks=recorded_earlier_ticks,
            expected_group_token=wrong_token,
        )

        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": leader_ident.pid,
                            "child_pgid": leader_ident.pid,
                            "child_start_ticks": recorded_earlier_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                            "group_token": wrong_token,
                            "descendants": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.1)
        assert outcome.ok is False
        assert outcome.reaped == ()
        assert len(outcome.unresolved) == 1
        assert outcome.unresolved[0]["pgid"] == leader_ident.pid
        gc_now = read_process_identity(gc_pid)
        assert gc_now is not None and gc_now.state != "Z"
        saved = json.loads(reg.read_text(encoding="utf-8"))["children"]
        assert len(saved) == 1
    finally:
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_reap_orphaned_children_reaps_verified_leaderless_grandchild_by_token_and_descendant(tmp_path):
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    leader1, leader1_ident, gc1_pid, _ = _spawn_exiting_leader_with_grandchild(env=env)
    leader2, leader2_ident, gc2_pid, gc2_ident = _spawn_exiting_leader_with_grandchild()
    try:
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": leader1_ident.pid,
                            "child_pgid": leader1_ident.pid,
                            "child_start_ticks": leader1_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                            "group_token": token,
                            "descendants": [],
                        },
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": leader2_ident.pid,
                            "child_pgid": leader2_ident.pid,
                            "child_start_ticks": leader2_ident.start_ticks,
                            "own_group": True,
                            "provider": "openai",
                            "group_token": None,
                            "descendants": [[gc2_ident.pid, gc2_ident.start_ticks]],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.2)
        assert outcome.ok is True
        assert {entry["pgid"] for entry in outcome.reaped} == {
            leader1_ident.pid,
            leader2_ident.pid,
        }
        assert read_process_identity(gc1_pid) is None or read_process_identity(gc1_pid).state == "Z"
        assert read_process_identity(gc2_pid) is None or read_process_identity(gc2_pid).state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        for pid in (gc1_pid, gc2_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(leader1, gc1_pid)
        _cleanup_proc(leader2, gc2_pid)


def test_reap_orphaned_children_reports_remaining_on_signal_failure(tmp_path, monkeypatch):
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    orphan, _ = _spawn_sleeper()
    try:
        orphan_ident = read_process_identity(orphan.pid)
        assert orphan_ident is not None
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": orphan_ident.pid,
                            "child_pgid": orphan_ident.pid,
                            "child_start_ticks": orphan_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.05)
        assert outcome.ok is False
        assert outcome.reaped == ()
        assert len(outcome.remaining) == 1
        assert outcome.remaining[0]["pid"] == orphan.pid
        assert orphan.poll() is None
        saved = json.loads(reg.read_text(encoding="utf-8"))["children"]
        assert len(saved) == 1
        assert saved[0]["child_pid"] == orphan.pid
    finally:
        _cleanup_proc(orphan)


def test_unregister_if_dead_preserves_entry_while_verified_grandchild_lives(tmp_path):
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}])\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.15)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="anthropic",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    leader.wait(timeout=2)
    try:
        assert (
            unregister_if_dead(
                ident.pid,
                pgid=ident.pid,
                start_ticks=ident.start_ticks,
                group_token=token,
                registry_path=reg,
            )
            is False
        )
        assert len(json.loads(reg.read_text(encoding="utf-8"))["children"]) == 1
    finally:
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_helios_state_dir_redirects_default_registry_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    assert child_registry.REGISTRY_PATH == tmp_path / "live-children.json"


def test_register_child_raises_persistence_error_on_write_failure(tmp_path, monkeypatch):
    """Pass 2 Finding 2: write failure must raise instead of silently returning identity."""
    reg = tmp_path / "live-children.json"
    proc, _ = _spawn_sleeper()
    try:
        monkeypatch.setattr(
            child_registry,
            "_write_entries_locked",
            lambda _path, _entries: "simulated ENOSPC",
        )
        with pytest.raises(ChildRegistryPersistenceError, match="ENOSPC"):
            register_child(proc.pid, pgid=proc.pid, own_group=True, registry_path=reg)
    finally:
        _cleanup_proc(proc)


def test_lock_timeout_bounds_reap_owned_and_register_without_hanging(tmp_path):
    """Pass 2 Finding 4: held flock and in-process lock must time out boundedly."""
    import threading

    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    reg.write_text(json.dumps({"version": 2, "children": []}), encoding="utf-8")
    lock_file = tmp_path / ".live-children.json.lock"

    # Hold flock(LOCK_EX) from a separate OS process.
    holder_code = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(lock_file)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().decode().strip() == "LOCKED"
    proc, _ = _spawn_sleeper()
    try:
        t0 = time.monotonic()
        outcome = reap_orphaned_children(
            registry_path=reg, grace_seconds=0.1, lock_timeout=0.05
        )
        assert time.monotonic() - t0 < 0.8
        assert outcome.ok is False
        assert outcome.error is not None and "timed out" in outcome.error

        t0 = time.monotonic()
        assert owned_child_targets(registry_path=reg, lock_timeout=0.05) == []
        assert time.monotonic() - t0 < 0.8

        t0 = time.monotonic()
        with pytest.raises(ChildRegistryPersistenceError, match="timed out"):
            register_child(
                proc.pid,
                pgid=proc.pid,
                own_group=True,
                registry_path=reg,
                lock_timeout=0.05,
            )
        assert time.monotonic() - t0 < 0.8
    finally:
        holder.kill()
        holder.wait(timeout=2)
        _cleanup_proc(proc)

    # Also verify bounded _INPROC_LOCK timeout when another thread holds it.
    locked_evt = threading.Event()
    release_evt = threading.Event()

    def hold_inproc():
        with child_registry._INPROC_LOCK:
            locked_evt.set()
            release_evt.wait(timeout=5)

    th = threading.Thread(target=hold_inproc, daemon=True)
    th.start()
    assert locked_evt.wait(timeout=2)
    try:
        t0 = time.monotonic()
        outcome = reap_orphaned_children(
            registry_path=reg, grace_seconds=0.1, lock_timeout=0.05
        )
        assert time.monotonic() - t0 < 0.8
        assert outcome.ok is False
        assert outcome.error is not None and "timed out" in outcome.error
    finally:
        release_evt.set()
        th.join(timeout=2)


def test_wait_for_process_group_captures_real_setsid_child():
    """Pass 2 Finding 1: wait_for_process_group waits for setsid wrapper."""
    import shutil

    setsid_path = shutil.which("setsid")
    if not setsid_path:
        pytest.skip("setsid binary not installed")
    proc = subprocess.Popen(
        [setsid_path, sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pgid = wait_for_process_group(proc.pid, expected_new_session=True, timeout=1.0)
        assert pgid == proc.pid
    finally:
        _cleanup_proc(proc)


def test_abort_unregistered_child_kills_sigterm_spawned_descendant_and_latches_on_failure(
    tmp_path, monkeypatch
):
    """Pass 3 Finding 2: abort kills SIGTERM-spawned leaderless descendants and latches failures."""
    from helios.backend.process.child_registry import (
        abort_unregistered_child,
        has_uncleaned_aborts,
        incomplete_cleanup_reason,
        retry_uncleaned_aborts,
    )

    # Part A: leader spawns a SIGTERM-ignoring grandchild INSIDE its SIGTERM handler and exits.
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_pid_file = tmp_path / "sigterm_gc.pid"
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import os, signal, subprocess, sys, time\n"
        "def on_term(_sig, _frame):\n"
        f"    gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}])\n"
        f"    open({str(gc_pid_file)!r}, 'w').write(str(gc.pid))\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().decode().strip() == "READY"
    gc_pid = None
    proc_ident = read_process_identity(proc.pid)
    assert proc_ident is not None
    try:
        cleared = abort_unregistered_child(
            proc.pid,
            pgid=proc.pid,
            start_ticks=proc_ident.start_ticks,
            group_token=token,
            poll=proc.poll,
            timeout=0.15,
            kill_wait=0.4,
        )
        assert cleared is True
        assert has_uncleaned_aborts() is False
        assert gc_pid_file.exists()
        gc_pid = int(gc_pid_file.read_text(encoding="utf-8").strip())
        gc_ident = read_process_identity(gc_pid)
        assert gc_ident is None or gc_ident.state == "Z"
    finally:
        if gc_pid is not None:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(proc, gc_pid)

    # Part B: incomplete abort latches global block until retry clears the survivor.
    token2 = new_group_token()
    env2 = dict(os.environ)
    env2[CHILD_GROUP_ENV] = token2
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env2,
        start_new_session=True,
    )
    try:
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            cleared2 = abort_unregistered_child(
                stubborn.pid,
                pgid=stubborn.pid,
                group_token=token2,
                poll=stubborn.poll,
                timeout=0.02,
                kill_wait=0.05,
            )
            assert cleared2 is False
            assert has_uncleaned_aborts() is True
            assert "aborted launch" in incomplete_cleanup_reason()

        # Restoring signalling lets retry_uncleaned_aborts reap and clear the latch.
        assert retry_uncleaned_aborts(timeout=0.3) is True
        assert has_uncleaned_aborts() is False
        assert incomplete_cleanup_reason() == ""
        stubborn.wait(timeout=2)
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        _cleanup_proc(stubborn)


def test_reap_process_trees_respects_shared_deadline_with_dead_targets_and_held_lock(tmp_path):
    """Pass 3 Finding 3: dead-target unregisters must not multiply lock waits or delay SIGTERM."""
    import threading

    reg = tmp_path / "live-children.json"
    reg.write_text(json.dumps({"version": 2, "children": []}), encoding="utf-8")

    # Spawn 4 quick-exiting processes to get real dead PIDs, plus 1 live child.
    dead_targets = []
    for _ in range(4):
        p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        ident = read_process_identity(p.pid)
        p.wait(timeout=2)
        dead_targets.append(
            ProcessTreeTarget(
                pid=p.pid,
                pgid=p.pid,
                start_ticks=ident.start_ticks if ident is not None else 1,
                registry_path=reg,
            )
        )
    live_proc, _ = _spawn_sleeper(ignore_term=False)
    live_ident = read_process_identity(live_proc.pid)
    assert live_ident is not None
    live_target = ProcessTreeTarget(
        pid=live_proc.pid,
        pgid=live_proc.pid,
        start_ticks=live_ident.start_ticks,
        poll=live_proc.poll,
        registry_path=reg,
    )

    locked_evt = threading.Event()
    release_evt = threading.Event()

    def hold_lock():
        with child_registry._INPROC_LOCK:
            locked_evt.set()
            release_evt.wait(timeout=5)

    th = threading.Thread(target=hold_lock, daemon=True)
    th.start()
    assert locked_evt.wait(timeout=2)
    try:
        # 4 dead targets alone with held lock must stay inside timeout + kill_wait.
        t0 = time.monotonic()
        reap_process_trees(dead_targets, timeout=0.02, kill_wait=0.02, unregister=True)
        assert time.monotonic() - t0 < 0.25

        # 4 dead targets + 1 live target: live target must STILL be signalled & reaped promptly!
        t1 = time.monotonic()
        res = reap_process_trees(
            [*dead_targets, live_target],
            timeout=0.15,
            kill_wait=0.05,
            send_term=True,
            unregister=True,
        )
        elapsed = time.monotonic() - t1
        assert elapsed < 0.5
        assert len(res.reaped) == 1
        assert res.reaped[0].pid == live_proc.pid
        live_proc.wait(timeout=2)
    finally:
        release_evt.set()
        th.join(timeout=2)
        _cleanup_proc(live_proc)


def test_abort_unregistered_child_never_adopts_recycled_pid_identity(tmp_path, monkeypatch):
    """Pass 4 Finding 1: abort must not signal a recycled PID with mismatched token or ticks."""
    from helios.backend.process.child_registry import abort_unregistered_child

    unrelated, _ = _spawn_sleeper(ignore_term=False)
    unrelated_ident = read_process_identity(unrelated.pid)
    assert unrelated_ident is not None
    wrong_token = new_group_token()
    try:
        assert (
            abort_unregistered_child(
                unrelated.pid,
                pgid=unrelated.pid,
                group_token=wrong_token,
                timeout=0.05,
                kill_wait=0.05,
            )
            is True
        )
        assert unrelated.poll() is None

        assert (
            abort_unregistered_child(
                unrelated.pid,
                pgid=unrelated.pid,
                start_ticks=unrelated_ident.start_ticks + 99999,
                group_token=wrong_token,
                timeout=0.05,
                kill_wait=0.05,
            )
            is True
        )
        assert unrelated.poll() is None

        reg = tmp_path / "live-children.json"
        monkeypatch.setattr(
            child_registry,
            "_write_entries_locked",
            lambda _path, _entries: "disk full",
        )
        with pytest.raises(ChildRegistryPersistenceError) as exc_info:
            register_child(
                unrelated.pid,
                pgid=unrelated.pid,
                own_group=True,
                group_token=wrong_token,
                registry_path=reg,
            )
        assert exc_info.value.child_identity == unrelated_ident
    finally:
        _cleanup_proc(unrelated)


def test_escaped_new_session_descendant_is_reaped_on_shutdown_and_orphan_recovery(
    tmp_path, monkeypatch
):
    """Pass 4 Finding 2: start_new_session=True descendants survive empty original PGID."""
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None

    def spawn_escaped(token: str):
        env = dict(os.environ)
        env[CHILD_GROUP_ENV] = token
        gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        leader_code = (
            "import subprocess, sys, time\n"
            f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
            "print(gc.pid, flush=True)\n"
            "time.sleep(0.05)\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        leader_ident = read_process_identity(leader.pid)
        assert leader_ident is not None
        assert leader.stdout is not None
        gc_pid = int(leader.stdout.readline().decode().strip())
        leader.wait(timeout=2)
        gc_ident = read_process_identity(gc_pid)
        assert gc_ident is not None and gc_ident.pgid != leader_ident.pid
        return leader, leader_ident, gc_pid

    tok1 = new_group_token()
    l1, l1_ident, gc1_pid = spawn_escaped(tok1)
    tok2 = new_group_token()
    l2, l2_ident, gc2_pid = spawn_escaped(tok2)
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)
    try:
        assert (
            classify_process_tree(
                l1_ident.pid,
                l1_ident.pid,
                expected_start_ticks=l1_ident.start_ticks,
                expected_group_token=tok1,
            )
            == "live"
        )
        assert (
            unregister_if_dead(
                l1_ident.pid,
                pgid=l1_ident.pid,
                start_ticks=l1_ident.start_ticks,
                group_token=tok1,
                registry_path=reg,
            )
            is False
        )
        target1 = ProcessTreeTarget(
            pid=l1_ident.pid,
            pgid=l1_ident.pid,
            start_ticks=l1_ident.start_ticks,
            group_token=tok1,
            registry_path=reg,
        )
        res = reap_process_trees([target1], timeout=0.15, kill_wait=0.3, send_term=True)
        assert len(res.reaped) == 1
        assert res.remaining == ()
        gc1_now = read_process_identity(gc1_pid)
        assert gc1_now is None or gc1_now.state == "Z"

        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": l2_ident.pid,
                            "child_pgid": l2_ident.pid,
                            "child_start_ticks": l2_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                            "group_token": tok2,
                            "descendants": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            failed = reap_orphaned_children(registry_path=reg, grace_seconds=0.05)
            assert failed.ok is False
            assert len(failed.remaining) == 1
            assert len(json.loads(reg.read_text(encoding="utf-8"))["children"]) == 1

        succeeded = reap_orphaned_children(registry_path=reg, grace_seconds=0.15)
        assert succeeded.ok is True
        assert len(succeeded.reaped) == 1
        gc2_now = read_process_identity(gc2_pid)
        assert gc2_now is None or gc2_now.state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        for pid in (gc1_pid, gc2_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(l1, gc1_pid)
        _cleanup_proc(l2, gc2_pid)


def test_exec_looping_escaped_descendant_cannot_evade_reap_on_transient_environ_miss(
    tmp_path, monkeypatch
):
    """Pass 5 Finding 2: retained (pid, start_ticks) catches escaped children across exec/environ gaps."""
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None

    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    # Escaped start_new_session=True child ignores SIGTERM and repeatedly execs /bin/sh.
    loop_sh = "trap '' TERM; while true; do /bin/sh -c 'true'; done"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen(['/bin/sh', '-c', {loop_sh!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None and gc_ident.pgid != leader_ident.pid

    # Simulate transient /proc/<pid>/environ misses after the very first scan discovers gc_pid!
    orig_scan = child_registry.scan_token_members
    calls = {"n": 0}

    def flaky_scan(tok):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_scan(tok)
        return []

    try:
        target = ProcessTreeTarget(
            pid=leader_ident.pid,
            pgid=leader_ident.pid,
            start_ticks=leader_ident.start_ticks,
            group_token=token,
            registry_path=reg,
        )
        # Even if scan_token_members misses on every poll after initial discovery,
        # retained (gc_pid, gc_ident.start_ticks) keeps the target live and SIGKILLs it!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", flaky_scan)
            res = reap_process_trees([target], timeout=0.1, kill_wait=0.35, send_term=True)
            assert len(res.reaped) == 1
            assert res.remaining == ()
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_driver_reap_stopped_preserves_and_reaps_escaped_descendant(tmp_path, monkeypatch):
    """Pass 5 Finding 1: Gemini/Muse/Kimi _reap_stopped must not delete registry row while escaped child lives."""
    pytest.importorskip("gi")
    from helios.backend.process.gemini_driver import GeminiCliDriver
    from helios.backend.process.kimi_driver import KimiAcpDriver
    from helios.backend.process.muse_driver import MuseServeDriver

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    reg = tmp_path / "live-children.json"

    for driver_cls in (GeminiCliDriver, MuseServeDriver, KimiAcpDriver):
        token = new_group_token()
        env = dict(os.environ)
        env[CHILD_GROUP_ENV] = token
        gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        leader_code = (
            "import subprocess, sys, time\n"
            f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
            "print(gc.pid, flush=True)\n"
            "time.sleep(0.15)\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        assert leader.stdout is not None
        gc_pid = int(leader.stdout.readline().decode().strip())
        ident = register_child(
            leader.pid,
            pgid=leader.pid,
            own_group=True,
            provider="test",
            group_token=token,
            registry_path=reg,
        )
        assert ident is not None
        try:
            # First simulate signalling blocked: _reap_stopped must NOT delete the row!
            with monkeypatch.context() as mp:
                mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
                driver_cls._reap_stopped(
                    leader,
                    start_ticks=ident.start_ticks,
                    group_token=token,
                )
            rows = json.loads(reg.read_text(encoding="utf-8"))["children"]
            assert any(r["child_pid"] == ident.pid for r in rows), driver_cls.__name__
            assert read_process_identity(gc_pid) is not None

            # Then allow signalling: _reap_stopped kills the escaped descendant and removes row!
            driver_cls._reap_stopped(
                leader,
                start_ticks=ident.start_ticks,
                group_token=token,
            )
            gc_now = read_process_identity(gc_pid)
            assert gc_now is None or gc_now.state == "Z", driver_cls.__name__
            rows_after = json.loads(reg.read_text(encoding="utf-8"))["children"]
            assert not any(r["child_pid"] == ident.pid for r in rows_after), driver_cls.__name__
        finally:
            try:
                os.killpg(gc_pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass
            _cleanup_proc(leader, gc_pid)


def test_token_miss_between_classification_and_reap_still_kills_escaped_descendant(
    tmp_path, monkeypatch
):
    """Pass 6 Finding 1: preliminary inspect_process_tree carries discovered descendants into reap."""
    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    try:
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": leader_ident.pid,
                            "child_pgid": leader_ident.pid,
                            "child_start_ticks": leader_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                            "group_token": token,
                            "descendants": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        orig_scan = child_registry.scan_token_members
        calls = {"n": 0}

        def only_first_scan(tok):
            calls["n"] += 1
            if calls["n"] == 1:
                return orig_scan(tok)
            return []

        monkeypatch.setattr(child_registry, "scan_token_members", only_first_scan)
        outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.15)
        assert outcome.ok is True
        assert len(outcome.reaped) == 1
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_retry_uncleaned_aborts_preserves_latched_descendants_across_token_misses(
    monkeypatch,
):
    """Pass 6 Finding 2: retry_uncleaned_aborts must preserve target.descendants when token scan misses."""
    from helios.backend.process.child_registry import (
        abort_unregistered_child,
        has_uncleaned_aborts,
        retry_uncleaned_aborts,
    )

    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    try:
        # Latch an uncleaned abort with gc captured in descendants.
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            cleared = abort_unregistered_child(
                leader_ident.pid,
                pgid=leader_ident.pid,
                start_ticks=leader_ident.start_ticks,
                group_token=token,
                descendants=((gc_ident.pid, gc_ident.start_ticks),),
                timeout=0.02,
                kill_wait=0.05,
            )
            assert cleared is False
            assert has_uncleaned_aborts() is True

        # Retry with token scan returning [] AND signalling still blocked:
        # latch MUST remain set and keep gc_ident alive in descendants!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", lambda _tok: [])
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            assert retry_uncleaned_aborts(timeout=0.05) is False
            assert has_uncleaned_aborts() is True

        # Retry with token scan STILL returning [] but signalling allowed:
        # retained descendants kill gc_pid and clear the latch!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", lambda _tok: [])
            assert retry_uncleaned_aborts(timeout=0.3) is True
            assert has_uncleaned_aborts() is False
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_incomplete_direct_reap_persists_discovered_descendants_for_crash_recovery(
    tmp_path, monkeypatch
):
    """Pass 6 Finding 3: direct reap persists discovered escaped descendants to live-children.json."""
    pytest.importorskip("gi")
    from helios.backend.process.gemini_driver import GeminiCliDriver

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.15)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="google",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)
    try:
        # Direct _reap_stopped runs while signalling fails -> must persist [gc_pid, gc_ticks]!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            GeminiCliDriver._reap_stopped(
                leader,
                start_ticks=ident.start_ticks,
                group_token=token,
            )
        payload = json.loads(reg.read_text(encoding="utf-8"))
        assert len(payload["children"]) == 1
        saved_desc = payload["children"][0]["descendants"]
        assert [gc_ident.pid, gc_ident.start_ticks] in saved_desc

        # Simulate crash recovery where owner died AND subsequent token scans return [].
        payload["children"][0]["owner_pid"] = dead_owner_ident.pid
        payload["children"][0]["owner_start_ticks"] = dead_owner_ident.start_ticks
        reg.write_text(json.dumps(payload), encoding="utf-8")

        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", lambda _tok: [])
            outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.15)
            assert outcome.ok is True
            assert len(outcome.reaped) == 1
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_classification_only_token_hit_retains_discovered_descendant_through_reap(
    tmp_path, monkeypatch
):
    """Pass 7 Finding 1: when refresh misses and only classification's scan hits, descendant is retained."""
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    try:
        orig_scan = child_registry.scan_token_members
        calls = {"n": 0}

        # Call 1 = _refresh_target_descendants inside inspect_process_tree -> MISS ([])
        # Call 2 = _classify_process_tree_detailed inside inspect_process_tree -> HIT!
        # Call 3+ = all subsequent scans -> MISS ([])
        def second_call_only(tok):
            calls["n"] += 1
            if calls["n"] == 2:
                return orig_scan(tok)
            return []

        monkeypatch.setattr(child_registry, "scan_token_members", second_call_only)
        target = ProcessTreeTarget(
            pid=leader_ident.pid,
            pgid=leader_ident.pid,
            start_ticks=leader_ident.start_ticks,
            group_token=token,
            registry_path=reg,
        )
        # reap_process_trees initially calls _refresh_target_descendants (call 0 if we account for it),
        # so test inspect_process_tree directly + pass enriched target into reap_process_trees with scans=[].
        status, enriched = child_registry.inspect_process_tree(target)
        assert status == "live"
        assert any(dpid == gc_pid for dpid, _ in enriched.descendants)

        # Even with ALL further token scans returning [], reaping enriched target kills gc_pid!
        monkeypatch.setattr(child_registry, "scan_token_members", lambda _tok: [])
        res = reap_process_trees([enriched], timeout=0.1, kill_wait=0.35, send_term=True)
        assert len(res.reaped) == 1
        assert res.remaining == ()
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_unregister_if_dead_honors_durably_recorded_descendants_when_env_scrubbed(
    tmp_path,
):
    """Pass 7 Finding 2: unregister_if_dead loads stored descendants and keeps row alive."""
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    # Grandchild starts in a NEW session with SCRUBBED env (no HELIOS_CHILD_GROUP)!
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import os, subprocess, sys, time\n"
        "clean_env = {k: v for k, v in os.environ.items() if k != 'HELIOS_CHILD_GROUP'}\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], env=clean_env, start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.15)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="anthropic",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    leader.wait(timeout=2)
    try:
        # Durably record gc_ident in live-children.json (as an earlier snapshot/reap would).
        payload = json.loads(reg.read_text(encoding="utf-8"))
        payload["children"][0]["descendants"] = [[gc_ident.pid, gc_ident.start_ticks]]
        reg.write_text(json.dumps(payload), encoding="utf-8")

        # Driver _on_exit calls unregister_if_dead WITHOUT passing descendants:
        # must load stored descendants from live-children.json and KEEP the row!
        removed = unregister_if_dead(
            ident.pid,
            pgid=ident.pid,
            start_ticks=ident.start_ticks,
            group_token=token,
            registry_path=reg,
        )
        assert removed is False
        assert len(json.loads(reg.read_text(encoding="utf-8"))["children"]) == 1

        # Once the stored descendant dies, unregister_if_dead removes the row cleanly.
        os.kill(gc_pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (read_process_identity(gc_pid) is not None):
            time.sleep(0.02)
        assert (
            unregister_if_dead(
                ident.pid,
                pgid=ident.pid,
                start_ticks=ident.start_ticks,
                group_token=token,
                registry_path=reg,
            )
            is True
        )
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_batch_removal_revalidates_stored_descendants_and_reaps_or_retains(
    tmp_path, monkeypatch
):
    """Pass 8 Finding 1: direct reap with stale target must not delete row while stored descendant lives."""
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    # Escaped child in a new session with scrubbed env (no HELIOS_CHILD_GROUP).
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import os, subprocess, sys, time\n"
        "clean_env = {k: v for k, v in os.environ.items() if k != 'HELIOS_CHILD_GROUP'}\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], env=clean_env, start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.15)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="anthropic",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    leader.wait(timeout=2)
    try:
        # Persist gc_ident in live-children.json, then pass a target WITHOUT descendants.
        payload = json.loads(reg.read_text(encoding="utf-8"))
        payload["children"][0]["descendants"] = [[gc_ident.pid, gc_ident.start_ticks]]
        reg.write_text(json.dumps(payload), encoding="utf-8")

        stale_target = ProcessTreeTarget(
            pid=ident.pid,
            pgid=ident.pid,
            start_ticks=ident.start_ticks,
            group_token=token,
            descendants=(),
            registry_path=reg,
        )
        # First with signalling blocked: row MUST NOT be deleted and remaining MUST report survivor!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            res = reap_process_trees([stale_target], timeout=0.05, kill_wait=0.05, unregister=True)
            assert res.reaped == ()
            assert len(res.remaining) == 1
            assert len(json.loads(reg.read_text(encoding="utf-8"))["children"]) == 1

        # With signalling enabled: direct reap kills the stored descendant and cleans up row!
        res2 = reap_process_trees([stale_target], timeout=0.1, kill_wait=0.3, unregister=True)
        assert len(res2.reaped) == 1
        assert res2.remaining == ()
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_leaderless_group_classification_propagates_token_discovered_members(
    tmp_path, monkeypatch
):
    """Pass 8 Finding 2: _classify_group token hit populates target.descendants so subsequent misses stay live."""
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    leader, leader_ident, gc_pid, gc_ident = _spawn_exiting_leader_with_grandchild(env=env)
    try:
        # Refresh scan returns [] (missed), while _classify_group's _process_has_group_token succeeds!
        monkeypatch.setattr(child_registry, "scan_token_members", lambda _tok: [])
        target = ProcessTreeTarget(
            pid=leader_ident.pid,
            pgid=leader_ident.pid,
            start_ticks=leader_ident.start_ticks,
            group_token=token,
            descendants=(),
        )
        status, enriched = child_registry.inspect_process_tree(target)
        assert status == "live"
        assert (gc_ident.pid, gc_ident.start_ticks) in enriched.descendants

        # Now even if _process_has_group_token ALSO fails (environ read miss), enriched target
        # stays "live" (not "unresolved") and reaps gc_pid!
        monkeypatch.setattr(child_registry, "_process_has_group_token", lambda *_a: False)
        status2, _ = child_registry.inspect_process_tree(enriched)
        assert status2 == "live"
        res = reap_process_trees([enriched], timeout=0.1, kill_wait=0.3, unregister=False)
        assert len(res.reaped) == 1
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_unregister_if_dead_latches_refreshed_target_when_descendant_write_fails(
    tmp_path, monkeypatch
):
    """Pass 8 Finding 3: failed persistence of discovered descendants in unregister_if_dead latches target."""
    from helios.backend.process.child_registry import (
        has_uncleaned_aborts,
        retry_uncleaned_aborts,
    )

    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    spawn_go = tmp_path / "spawn_gc.go"
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import pathlib, subprocess, sys, time\n"
        f"go = pathlib.Path({str(spawn_go)!r})\n"
        "deadline = time.monotonic() + 10.0\n"
        "while not go.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.1)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="anthropic",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    spawn_go.write_text("go", encoding="utf-8")
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    try:
        # Simulate ENOSPC when unregister_if_dead tries to persist newly discovered gc_pid.
        with monkeypatch.context() as mp:
            mp.setattr(
                child_registry,
                "_write_entries_locked",
                lambda _p, _e: "simulated ENOSPC",
            )
            removed = unregister_if_dead(
                ident.pid,
                pgid=ident.pid,
                start_ticks=ident.start_ticks,
                group_token=token,
                registry_path=reg,
            )
            assert removed is False
            assert has_uncleaned_aborts() is True

        # Retry kills gc_pid using the latched identity even if token scans now return []!
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", lambda _tok: [])
            assert retry_uncleaned_aborts(timeout=0.3) is True
            assert has_uncleaned_aborts() is False
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_escaped_session_leader_with_token_scrubbed_child_in_its_group_is_reaped(tmp_path):
    """Pass 9 Finding 1: snapshot members of escaped verified session groups before signalling."""
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token

    # Leader spawns an escaped session leader (keeps token, exits on SIGTERM), which spawns
    # a child in its own group with token SCRUBBED and SIGTERM ignored!
    scrub_gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    escaped_leader_code = (
        "import os, signal, subprocess, sys, time\n"
        "clean_env = {k: v for k, v in os.environ.items() if k != 'HELIOS_CHILD_GROUP'}\n"
        f"sub = subprocess.Popen([sys.executable, '-c', {scrub_gc_code!r}], env=clean_env)\n"
        "print(sub.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    root_code = (
        "import subprocess, sys, time\n"
        f"el = subprocess.Popen([sys.executable, '-c', {escaped_leader_code!r}], "
        "stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "sub_pid = el.stdout.readline().decode().strip()\n"
        "print(f'{el.pid} {sub_pid}', flush=True)\n"
        "time.sleep(0.05)\n"
    )
    root_proc = subprocess.Popen(
        [sys.executable, "-c", root_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    root_ident = read_process_identity(root_proc.pid)
    assert root_ident is not None
    assert root_proc.stdout is not None
    el_pid_s, sub_pid_s = root_proc.stdout.readline().decode().strip().split()
    el_pid, sub_pid = int(el_pid_s), int(sub_pid_s)
    root_proc.wait(timeout=2)
    try:
        assert read_process_identity(el_pid) is not None
        assert read_process_identity(sub_pid) is not None
        target = ProcessTreeTarget(
            pid=root_ident.pid,
            pgid=root_ident.pid,
            start_ticks=root_ident.start_ticks,
            group_token=token,
            registry_path=reg,
        )
        res = reap_process_trees([target], timeout=0.15, kill_wait=0.35, send_term=True)
        assert len(res.reaped) == 1
        assert res.remaining == ()
        assert read_process_identity(el_pid) is None or read_process_identity(el_pid).state == "Z"
        sub_now = read_process_identity(sub_pid)
        assert sub_now is None or sub_now.state == "Z"
    finally:
        for pid in (el_pid, sub_pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(root_proc)


def test_256_dead_target_descendants_cannot_evict_live_stored_descendant(tmp_path, monkeypatch):
    """Pass 9 Finding 2: cap must never drop a live stored descendant behind 256 dead target entries."""
    reg = tmp_path / "live-children.json"
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import os, subprocess, sys, time\n"
        "clean_env = {k: v for k, v in os.environ.items() if k != 'HELIOS_CHILD_GROUP'}\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], env=clean_env, start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.15)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    ident = register_child(
        leader.pid,
        pgid=leader.pid,
        own_group=True,
        provider="anthropic",
        group_token=token,
        registry_path=reg,
    )
    assert ident is not None
    gc_ident = read_process_identity(gc_pid)
    assert gc_ident is not None
    leader.wait(timeout=2)
    try:
        payload = json.loads(reg.read_text(encoding="utf-8"))
        payload["children"][0]["descendants"] = [[gc_ident.pid, gc_ident.start_ticks]]
        reg.write_text(json.dumps(payload), encoding="utf-8")

        # 256 synthetic dead descendants on the incoming target must NOT truncate gc_ident!
        dead_256 = tuple((3_000_000 + i, 1) for i in range(256))
        noisy_target = ProcessTreeTarget(
            pid=ident.pid,
            pgid=ident.pid,
            start_ticks=ident.start_ticks,
            group_token=token,
            descendants=dead_256,
            registry_path=reg,
        )
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            res = reap_process_trees([noisy_target], timeout=0.05, kill_wait=0.05, unregister=True)
            assert res.reaped == ()
            assert len(res.remaining) == 1
            rows = json.loads(reg.read_text(encoding="utf-8"))["children"]
            assert len(rows) == 1
            assert [gc_ident.pid, gc_ident.start_ticks] in rows[0]["descendants"]

        res2 = reap_process_trees([noisy_target], timeout=0.1, kill_wait=0.3, unregister=True)
        assert len(res2.reaped) == 1
        assert res2.remaining == ()
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
        assert json.loads(reg.read_text(encoding="utf-8"))["children"] == []
    finally:
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_signal_process_tree_discovery_retains_escaped_group_children(monkeypatch):
    """Pass 10 Finding 1: an escaped group first discovered inside signal_process_tree retains its children."""
    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token

    scrub_gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    escaped_leader_code = (
        "import os, subprocess, sys, time\n"
        "clean_env = {k: v for k, v in os.environ.items() if k != 'HELIOS_CHILD_GROUP'}\n"
        f"sub = subprocess.Popen([sys.executable, '-c', {scrub_gc_code!r}], env=clean_env)\n"
        "print(sub.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    root_code = (
        "import subprocess, sys, time\n"
        f"el = subprocess.Popen([sys.executable, '-c', {escaped_leader_code!r}], "
        "stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "sub_pid = el.stdout.readline().decode().strip()\n"
        "print(f'{el.pid} {sub_pid}', flush=True)\n"
        "time.sleep(60)\n"
    )
    root_proc = subprocess.Popen(
        [sys.executable, "-c", root_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    root_ident = read_process_identity(root_proc.pid)
    assert root_ident is not None
    assert root_proc.stdout is not None
    el_pid_s, sub_pid_s = root_proc.stdout.readline().decode().strip().split()
    el_pid, sub_pid = int(el_pid_s), int(sub_pid_s)
    try:
        orig_scan = child_registry.scan_token_members
        calls = {"n": 0}

        def hit_only_on_sigterm(tok):
            calls["n"] += 1
            if calls["n"] == 3:
                return orig_scan(tok)
            return []

        monkeypatch.setattr(child_registry, "scan_token_members", hit_only_on_sigterm)
        target = ProcessTreeTarget(
            pid=root_ident.pid,
            pgid=root_ident.pid,
            start_ticks=root_ident.start_ticks,
            poll=root_proc.poll,
            group_token=token,
        )
        res = reap_process_trees([target], timeout=0.15, kill_wait=0.35, send_term=True, unregister=False)
        assert len(res.reaped) == 1
        assert res.remaining == ()
        sub_now = read_process_identity(sub_pid)
        assert sub_now is None or sub_now.state == "Z"
    finally:
        for pid in (el_pid, sub_pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(root_proc)


def test_snapshot_group_descendants_has_no_256_cap(monkeypatch):
    """Pass 10 Finding 2: live group snapshots must not truncate beyond 256 members."""
    fake_leader = child_registry.ProcessIdentity(
        pid=5000, state="S", ppid=1, pgid=5000, sid=5000, start_ticks=100, uid=os.getuid()
    )
    fake_members = [fake_leader] + [
        child_registry.ProcessIdentity(
            pid=5001 + i,
            state="S",
            ppid=5000,
            pgid=5000,
            sid=5000,
            start_ticks=101 + i,
            uid=os.getuid(),
        )
        for i in range(260)
    ]
    monkeypatch.setattr(child_registry, "read_process_identity", lambda pid: fake_leader if pid == 5000 else None)
    monkeypatch.setattr(child_registry, "_scan_group_members", lambda pgid: fake_members if pgid == 5000 else [])
    snap = child_registry.snapshot_group_descendants(5000, 5000, 100)
    assert len(snap) == 260
    assert (5260, 360) in snap


def test_orphan_reap_write_failure_latches_discovered_survivors(tmp_path, monkeypatch):
    """Pass 10 Finding 3: ENOSPC during orphan-reap persistence latches discovered survivors."""
    from helios.backend.process.child_registry import (
        has_uncleaned_aborts,
        retry_uncleaned_aborts,
    )

    reg = tmp_path / "live-children.json"
    boot_id = current_boot_id()
    assert boot_id is not None
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner_ident = read_process_identity(dead_owner.pid)
    assert dead_owner_ident is not None
    dead_owner.wait(timeout=2)

    token = new_group_token()
    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    gc_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}], start_new_session=True)\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(0.05)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    leader_ident = read_process_identity(leader.pid)
    assert leader_ident is not None
    assert leader.stdout is not None
    gc_pid = int(leader.stdout.readline().decode().strip())
    leader.wait(timeout=2)
    try:
        reg.write_text(
            json.dumps(
                {
                    "version": 2,
                    "children": [
                        {
                            "boot_id": boot_id,
                            "owner_pid": dead_owner_ident.pid,
                            "owner_start_ticks": dead_owner_ident.start_ticks,
                            "child_pid": leader_ident.pid,
                            "child_pgid": leader_ident.pid,
                            "child_start_ticks": leader_ident.start_ticks,
                            "own_group": True,
                            "provider": "anthropic",
                            "group_token": token,
                            "descendants": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "signal_process_tree", lambda *a, **kw: False)
            mp.setattr(
                child_registry,
                "_write_entries_locked",
                lambda _p, _e: "simulated ENOSPC",
            )
            outcome = reap_orphaned_children(registry_path=reg, grace_seconds=0.05)
            assert outcome.ok is False
            assert outcome.error == "simulated ENOSPC"
            assert has_uncleaned_aborts() is True

        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "scan_token_members", lambda _tok: [])
            assert retry_uncleaned_aborts(timeout=0.3) is True
            assert has_uncleaned_aborts() is False
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        try:
            os.killpg(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        _cleanup_proc(leader, gc_pid)


def test_stop_snapshots_descendants_before_sending_sigterm(monkeypatch):
    """Pass 11 Finding 1: Gemini/Muse/Kimi stop() must snapshot group members before SIGTERM."""
    pytest.importorskip("gi")
    from helios.backend.process.gemini_driver import GeminiCliDriver
    from helios.backend.process.kimi_driver import KimiAcpDriver
    from helios.backend.process.muse_driver import MuseServeDriver

    for driver_cls in (GeminiCliDriver, MuseServeDriver, KimiAcpDriver):
        events: list[str] = []
        fake_proc = type(
            "_P",
            (),
            {"pid": 54321, "poll": lambda self: None, "stdin": None},
        )()
        fake_ident = child_registry.ProcessIdentity(
            pid=54321, state="S", ppid=os.getpid(), pgid=54321, sid=54321, start_ticks=999, uid=os.getuid()
        )
        mod_name = driver_cls.__module__
        with monkeypatch.context() as mp:
            mp.setattr(
                f"{mod_name}.snapshot_group_descendants",
                lambda *_a, **_kw: (events.append("snapshot") or ((54322, 1000),)),
            )
            mp.setattr(
                f"{mod_name}.os.killpg",
                lambda *_a, **_kw: events.append("sigterm"),
            )
            mp.setattr(f"{mod_name}.wake_if_stopped", lambda *_a, **_kw: False)
            captured_kwargs: dict[str, object] = {}

            class _FakeThread:
                def __init__(self, *, target=None, args=(), kwargs=None, daemon=False):
                    captured_kwargs.update(kwargs or {})

                def start(self):
                    events.append("thread-start")

            mp.setattr(f"{mod_name}.threading.Thread", _FakeThread)
            drv = driver_cls.__new__(driver_cls)
            drv._stopped = False
            drv._proc = fake_proc
            drv._child_identity = fake_ident
            drv._child_group_token = new_group_token()
            drv._is_busy = False
            drv._session_id = ""
            drv._root_turn_id = ""
            drv._open_tool_calls = {}
            drv.background_task_rows = []
            drv._pending_requests = {}
            drv._pending_permissions = {}
            drv._interrupt_requested_turn = ""
            drv._cancel_liveness_watchdog = lambda: None
            drv._finish_predispatch_attempt = lambda: None
            drv._close_stdin_quietly = lambda: None
            drv._send = lambda *_a, **_kw: False
            drv.stop(interrupt=False)
        assert events[:2] == ["snapshot", "sigterm"], driver_cls.__name__
        assert captured_kwargs.get("descendants") == ((54322, 1000),), driver_cls.__name__


def test_retry_uncleaned_aborts_preserves_concurrently_latched_updates(monkeypatch):
    """Pass 11 Finding 2: generation compare-and-remove keeps concurrent latch updates."""
    from helios.backend.process.child_registry import (
        has_uncleaned_aborts,
        retry_uncleaned_aborts,
    )

    token = new_group_token()
    initial = ProcessTreeTarget(pid=700001, pgid=700001, start_ticks=10, group_token=token)
    concurrent_update = ProcessTreeTarget(
        pid=700001,
        pgid=700001,
        start_ticks=10,
        group_token=token,
        descendants=((700002, 20),),
    )
    try:
        child_registry._latch_unpersisted_targets([initial])
        assert has_uncleaned_aborts() is True

        injected = {"done": False}

        def fake_abort_detailed(*args, **kwargs):
            if not injected["done"]:
                injected["done"] = True
                child_registry._latch_unpersisted_targets([concurrent_update])
            return True, []

        monkeypatch.setattr(
            child_registry,
            "_abort_unregistered_child_detailed",
            fake_abort_detailed,
        )
        assert retry_uncleaned_aborts(timeout=0.05) is False
        assert has_uncleaned_aborts() is True
        with child_registry._ABORT_LATCH_LOCK:
            _, stored_target = next(iter(child_registry._UNREAPED_ABORT_TARGETS.values()))
            assert (700002, 20) in stored_target.descendants

        assert retry_uncleaned_aborts(timeout=0.05) is True
        assert has_uncleaned_aborts() is False
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()


def test_register_child_caps_and_latches_overflow_beyond_256_descendants(
    tmp_path, monkeypatch
):
    """Pass 11 Finding 3: register_child routes through storage pruning and latches >256 live overflow."""
    from helios.backend.process.child_registry import has_uncleaned_aborts

    reg = tmp_path / "live-children.json"
    proc, _ = _spawn_sleeper()
    proc_ident = read_process_identity(proc.pid)
    assert proc_ident is not None
    fake_260 = tuple((600000 + i, 50 + i) for i in range(260))
    fake_ident_map = {
        pid: child_registry.ProcessIdentity(
            pid=pid, state="S", ppid=proc.pid, pgid=proc.pid, sid=proc.pid, start_ticks=ticks, uid=os.getuid()
        )
        for pid, ticks in fake_260
    }
    orig_read = child_registry.read_process_identity
    try:
        with monkeypatch.context() as mp:
            mp.setattr(child_registry, "snapshot_group_descendants", lambda *_a: fake_260)
            mp.setattr(
                child_registry,
                "read_process_identity",
                lambda p: fake_ident_map[p] if p in fake_ident_map else orig_read(p),
            )
            ident = register_child(
                proc.pid, pgid=proc.pid, own_group=True, registry_path=reg
            )
            assert ident is not None
        rows = json.loads(reg.read_text(encoding="utf-8"))["children"]
        assert len(rows) == 1
        assert len(rows[0]["descendants"]) == 256
        assert has_uncleaned_aborts() is True
        with child_registry._ABORT_LATCH_LOCK:
            _, latched_target = next(iter(child_registry._UNREAPED_ABORT_TARGETS.values()))
            assert len(latched_target.descendants) == 4
            assert (600259, 309) in latched_target.descendants
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        _cleanup_proc(proc)


def test_register_child_write_failure_preserves_escaped_token_scrubbed_descendant(
    tmp_path, monkeypatch
):
    """Pass 12 Finding 1: persistence failure carries captured descendants so abort reaps or latches escaped survivors."""
    from helios.backend.process.child_registry import (
        abort_unregistered_child,
        has_uncleaned_aborts,
        retry_uncleaned_aborts,
    )

    reg = tmp_path / "live-children.json"
    token = new_group_token()
    child_pid_file = tmp_path / "desc.pid"
    escape_trigger = tmp_path / "escape.go"
    escaped_ready = tmp_path / "escaped.ready"
    leader_exit = tmp_path / "leader.exit"

    escaped_script = tmp_path / "escaped.py"
    escaped_script.write_text(
        "import pathlib, time\n"
        f"pathlib.Path({str(escaped_ready)!r}).write_text('ok', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    desc_script = tmp_path / "desc.py"
    desc_script.write_text(
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"trigger = pathlib.Path({str(escape_trigger)!r})\n"
        "deadline = time.monotonic() + 10.0\n"
        "while not trigger.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "os.setsid()\n"
        f"env = {{k: v for k, v in os.environ.items() if k != {CHILD_GROUP_ENV!r}}}\n"
        f"os.execve(sys.executable, [sys.executable, {str(escaped_script)!r}], env)\n",
        encoding="utf-8",
    )
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(desc_script)!r}])\n"
        f"stop = pathlib.Path({str(leader_exit)!r})\n"
        "deadline = time.monotonic() + 10.0\n"
        "while not stop.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env[CHILD_GROUP_ENV] = token
    leader = subprocess.Popen(
        [sys.executable, str(leader_script)],
        start_new_session=True,
        env=env,
    )
    desc_pid = None
    try:
        deadline = time.monotonic() + 5.0
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists()
        desc_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        desc_ident = read_process_identity(desc_pid)
        assert desc_ident is not None
        assert desc_ident.pgid == leader.pid

        def failing_write(_path, _entries):
            escape_trigger.write_text("go", encoding="utf-8")
            esc_deadline = time.monotonic() + 5.0
            while not escaped_ready.exists() and time.monotonic() < esc_deadline:
                time.sleep(0.01)
            assert escaped_ready.exists()
            leader_exit.write_text("bye", encoding="utf-8")
            leader.wait(timeout=5)
            return "simulated disk full"

        monkeypatch.setattr(child_registry, "_write_entries_locked", failing_write)
        with pytest.raises(ChildRegistryPersistenceError, match="disk full") as exc_info:
            register_child(
                leader.pid,
                pgid=leader.pid,
                own_group=True,
                group_token=token,
                registry_path=reg,
            )

        exc = exc_info.value
        assert exc.child_identity is not None
        assert (desc_pid, desc_ident.start_ticks) in exc.descendants

        # Verify the descendant truly escaped PGID and scrubbed the token.
        after = read_process_identity(desc_pid)
        assert after is not None and after.start_ticks == desc_ident.start_ticks
        assert after.pgid == desc_pid and after.pgid != leader.pid
        assert child_registry.scan_token_members(token) == []

        # First verify failure to signal latches the escaped descendant.
        orig_kill = os.kill
        orig_killpg = os.killpg
        with monkeypatch.context() as mp:
            mp.setattr(
                os,
                "kill",
                lambda p, s: (_ for _ in ()).throw(PermissionError("denied"))
                if p == desc_pid
                else orig_kill(p, s),
            )
            mp.setattr(
                os,
                "killpg",
                lambda pg, s: (_ for _ in ()).throw(PermissionError("denied"))
                if pg == desc_pid
                else orig_killpg(pg, s),
            )
            aborted = abort_unregistered_child(
                leader.pid,
                pgid=leader.pid,
                start_ticks=exc.child_identity.start_ticks,
                group_token=token,
                descendants=exc.descendants,
                poll=leader.poll,
                timeout=0.05,
                kill_wait=0.05,
            )
            assert aborted is False
            assert has_uncleaned_aborts() is True

        # Retry clears the latch and kills the escaped token-scrubbed descendant.
        assert retry_uncleaned_aborts(timeout=1.0) is True
        assert has_uncleaned_aborts() is False
        dead_deadline = time.monotonic() + 2.0
        while time.monotonic() < dead_deadline:
            check = read_process_identity(desc_pid)
            if check is None or check.start_ticks != desc_ident.start_ticks or check.state == "Z":
                break
            time.sleep(0.02)
        final_check = read_process_identity(desc_pid)
        assert (
            final_check is None
            or final_check.start_ticks != desc_ident.start_ticks
            or final_check.state == "Z"
        )
    finally:
        with child_registry._ABORT_LATCH_LOCK:
            child_registry._UNREAPED_ABORT_TARGETS.clear()
        if desc_pid is not None:
            try:
                os.killpg(desc_pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(desc_pid, signal.SIGKILL)
            except OSError:
                pass
        _cleanup_proc(leader)
