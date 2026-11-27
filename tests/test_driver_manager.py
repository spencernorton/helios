from __future__ import annotations

from helios.backend.process.driver_manager import DriverManager


class FakeDriver:
    def __init__(
        self,
        session_id: str = "",
        *,
        busy: bool = False,
        accepting: bool = True,
        last_activity: float = 0.0,
    ) -> None:
        self.session_id = session_id
        self.is_running = True
        self.is_busy = busy
        self.is_accepting_input = accepting
        self.last_activity = last_activity
        self.ended = 0
        self.started = 0
        self.stopped: list[bool] = []
        self.fail_start = False
        self._next_handler_id = 0
        self.disconnected: list[int] = []

    def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("spawn failed")

    def connect(self, _signal: str) -> int:
        self._next_handler_id += 1
        return self._next_handler_id

    def end_input(self) -> None:
        self.ended += 1
        self.is_accepting_input = False

    def disconnect(self, handler: int) -> None:
        self.disconnected.append(handler)

    def stop(self, *, interrupt: bool = True) -> None:
        self.stopped.append(interrupt)


def test_register_forget_and_live_ids_callback():
    seen: list[set[str]] = []
    mgr = DriverManager(
        max_live=4,
        idle_seconds=60,
        on_live_ids_changed=lambda ids: seen.append(ids),
    )
    driver = FakeDriver("s1")

    mgr.add_starting(driver, [10, 11])
    mgr.register_started(driver, "s1")

    assert mgr.current is driver
    assert mgr.starting is None
    assert mgr.live_ids() == {"s1"}
    assert seen[-1] == {"s1"}

    mgr.forget(driver)

    assert mgr.current is None
    assert mgr.live == {}
    assert driver.disconnected == [10, 11]
    assert seen[-1] == set()


def test_idle_reap_skips_current_busy_and_non_accepting():
    current = FakeDriver("current", last_activity=0)
    busy = FakeDriver("busy", busy=True, last_activity=0)
    closed = FakeDriver("closed", accepting=False, last_activity=0)
    stale = FakeDriver("stale", last_activity=0)
    fresh = FakeDriver("fresh", last_activity=95)
    mgr = DriverManager(max_live=10, idle_seconds=60)
    mgr.current = current
    mgr.live = {
        "current": current,
        "busy": busy,
        "closed": closed,
        "stale": stale,
        "fresh": fresh,
    }

    mgr.reap_idle(now=100)

    assert stale.ended == 1
    assert current.ended == busy.ended == closed.ended == fresh.ended == 0


def test_evict_for_cap_uses_lru_idle_background_only():
    current = FakeDriver("current", last_activity=0)
    oldest = FakeDriver("oldest", last_activity=1)
    middle = FakeDriver("middle", last_activity=2)
    busy = FakeDriver("busy", busy=True, last_activity=0)
    mgr = DriverManager(max_live=3, idle_seconds=60)
    mgr.current = current
    mgr.live = {
        "current": current,
        "oldest": oldest,
        "middle": middle,
        "busy": busy,
    }

    mgr.evict_for_cap()

    assert oldest.ended == 1
    assert middle.ended == 1
    assert busy.ended == 0


def test_gemini_start_waits_for_an_idle_process_to_exit():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    oldest = FakeDriver("old", last_activity=1)
    newer = FakeDriver("newer", last_activity=2)
    current = FakeDriver("current")
    starting = FakeDriver(accepting=False)
    starting.is_initializing = True
    for driver in (oldest, newer, current, starting):
        driver.provider = "google"
        mgr.add_starting(driver, [])
        mgr.register_started(driver, driver.session_id)
    mgr.bind_current(current)

    assert mgr.prepare_provider_start("google", 4) == "closing"
    assert oldest.ended == 1
    assert mgr.prepare_provider_start("google", 4) == "closing"
    assert newer.ended == current.ended == starting.ended == 0

    oldest.is_running = False  # OS exit precedes the GTK registry callback.
    assert mgr.prepare_provider_start("google", 4) == "ready"


def test_gemini_start_preserves_active_and_pending_turns():
    busy = FakeDriver("busy", busy=True)
    checkpoint = FakeDriver("checkpoint")
    queued = FakeDriver("queued")
    queued.queued_messages = lambda: [(1, "queued text")]
    starting = FakeDriver(accepting=False)
    starting.is_initializing = True
    claude = FakeDriver("claude")
    claude.provider = "anthropic"
    mgr = DriverManager(
        max_live=4, idle_seconds=60,
        has_pending_dispatch=lambda driver: driver is checkpoint,
    )
    for driver in (busy, checkpoint, queued, starting):
        driver.provider = "google"
        mgr.add_starting(driver, [])
        mgr.register_started(driver, driver.session_id)
    mgr.register_started(claude, claude.session_id)
    mgr.bind_current(None)

    assert mgr.prepare_provider_start("google", 4) == "busy"
    assert all(d.ended == 0 for d in (busy, checkpoint, queued, starting, claude))
    busy.is_running = False
    assert mgr.prepare_provider_start("google", 4) == "ready"
    assert claude.ended == 0


def test_gemini_start_counts_a_detached_process_until_it_exits():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    closing = FakeDriver("closing")
    closing.provider = "google"
    mgr.add_starting(closing, [])
    mgr.register_started(closing, closing.session_id)
    mgr.teardown(closing)

    assert not mgr.live_ids()
    assert mgr.prepare_provider_start("google", 1) == "closing"
    closing.is_running = False
    assert mgr.prepare_provider_start("google", 1) == "ready"


def test_provider_helpers():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("c")
    claude.provider = "anthropic"
    gpt = FakeDriver("g")
    gpt.provider = "openai"
    unknown = FakeDriver("u")
    mgr.current = gpt

    assert mgr.driver_matches_provider(claude, "anthropic")
    assert not mgr.driver_matches_provider(claude, "openai")
    assert mgr.driver_provider(unknown) == ""
    assert not mgr.driver_matches_provider(unknown, "anthropic")
    assert mgr.current_accepting_matches_provider("openai")
    gpt.is_accepting_input = False
    assert not mgr.current_accepting_matches_provider("openai")


def test_start_new_registers_starting_and_cleans_up_on_start_failure():
    seen: list[set[str]] = []
    mgr = DriverManager(
        max_live=4,
        idle_seconds=60,
        on_live_ids_changed=lambda ids: seen.append(ids),
    )
    driver = FakeDriver("starting")
    driver.fail_start = True

    try:
        mgr.start_new(lambda: driver, lambda d: [d.connect("x")])
    except RuntimeError as e:
        assert "spawn failed" in str(e)
    else:
        raise AssertionError("start_new should re-raise start failure")

    assert driver.started == 1
    assert driver.disconnected == [1]
    assert mgr.current is None
    assert mgr.starting is None
    assert seen[-1] == set()


def test_start_new_success_returns_driver():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    driver = FakeDriver("starting")

    started = mgr.start_new(lambda: driver, lambda d: [d.connect("x")])

    assert started is driver
    assert driver.started == 1
    assert mgr.current is driver
    assert mgr.starting is driver


def test_teardown_forgets_then_stops_driver_without_interrupt_by_default():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    driver = FakeDriver("s1")
    mgr.current = driver
    mgr.live = {"s1": driver}
    mgr.handlers[driver] = [1]

    old = mgr.teardown()

    assert old is driver
    assert mgr.current is None
    assert mgr.live == {}
    assert driver.disconnected == [1]
    assert driver.stopped == [False]


def test_stop_all_includes_starting_driver():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    live = FakeDriver("live")
    starting = FakeDriver("starting")
    mgr.live = {"live": live}
    mgr.starting = starting

    mgr.stop_all(interrupt=False)

    assert live.stopped == [False]
    assert starting.stopped == [False]


def test_work_participant_lookup_finds_driver_before_native_id_arrives():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    starting = FakeDriver()
    starting._helios_work_id = "work-1"
    starting._helios_participant_provider = "openai"
    starting._helios_participant_generation = 2
    mgr.add_starting(starting, [1])

    assert mgr.driver_for_session("") is None
    assert mgr.driver_for_work_participant("work-1", "openai") is starting
    assert (
        mgr.driver_for_work_participant("work-1", "openai", generation=2)
        is starting
    )
    assert mgr.driver_for_work_participant("work-1", "openai", generation=1) is None
    assert mgr.driver_for_work_participant("work-1", "anthropic") is None


def test_work_sibling_lookup_keeps_provider_native_bindings_distinct():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("claude-native")
    claude.provider = "anthropic"
    claude._helios_work_id = "work-1"
    claude._helios_participant_provider = "anthropic"
    claude._helios_participant_generation = 1
    gpt = FakeDriver("gpt-native")
    gpt.provider = "openai"
    gpt._helios_work_id = "work-1"
    gpt._helios_participant_provider = "openai"
    gpt._helios_participant_generation = 1
    mgr.handlers = {claude: [1], gpt: [2]}
    mgr.live = {claude.session_id: claude, gpt.session_id: gpt}

    assert mgr.driver_for_work_participant("work-1", "anthropic") is claude
    assert mgr.driver_for_work_participant("work-1", "openai") is gpt

    mgr.bind_current(gpt)
    mgr.bind_current(claude)

    assert mgr.driver_for_session("claude-native") is claude
    assert mgr.driver_for_session("gpt-native") is gpt
    assert mgr.live_ids() == {"claude-native", "gpt-native"}


def test_shutdown_includes_all_handlers_when_multiple_providers_are_starting():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver()
    codex = FakeDriver()
    mgr.add_starting(claude, [1])
    mgr.add_starting(codex, [2])  # legacy pointer now references only Codex

    mgr.stop_all(interrupt=False)

    assert claude.stopped == [False]
    assert codex.stopped == [False]


def test_drivers_for_work_includes_live_and_starting_siblings_only():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("claude-native")
    claude._helios_work_id = "work-1"
    codex = FakeDriver()
    codex._helios_work_id_hint = "work-1"
    unrelated = FakeDriver("other")
    unrelated._helios_work_id = "work-2"
    mgr.handlers = {claude: [1], codex: [2], unrelated: [3]}
    mgr.live = {claude.session_id: claude, unrelated.session_id: unrelated}
    mgr.starting = codex

    assert mgr.drivers_for_work("work-1") == {claude, codex}
    assert mgr.drivers_for_work("work-2") == {unrelated}
    assert mgr.drivers_for_work("") == set()


def test_initializing_resume_driver_is_found_by_session_and_work_before_session_bound():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    starting = FakeDriver("", accepting=False, busy=True)
    starting.is_initializing = True
    starting.provider = "muse"
    starting._helios_expected_resume_id = "muse-resume-1"
    starting._helios_work_id = "work-1"
    starting._helios_participant_provider = "muse"
    starting._helios_participant_generation = 1
    mgr.add_starting(starting, [1])

    assert mgr.driver_for_session("muse-resume-1") is starting
    assert mgr.driver_for_work_participant("work-1", "muse", generation=1) is starting
    assert mgr.live_ids() == {"muse-resume-1"}


def test_stop_all_wait_timeout_reaps_sigterm_ignoring_live_and_tearing_down_children(tmp_path):
    import subprocess
    import sys
    import pytest
    from helios.backend.process.child_registry import (
        ProcessTreeTarget,
        is_process_tree_live,
        register_child,
    )

    if sys.platform != "linux":
        pytest.skip("Linux /proc process identity required")

    def spawn_stubborn():
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            ],
            start_new_session=True,
        )

    reg = tmp_path / "live-children.json"
    p_live = spawn_stubborn()
    p_tearing = spawn_stubborn()
    try:
        id_live = register_child(p_live.pid, pgid=p_live.pid, own_group=True, registry_path=reg)
        id_tearing = register_child(p_tearing.pid, pgid=p_tearing.pid, own_group=True, registry_path=reg)
        assert id_live is not None and id_tearing is not None

        class RealFakeDriver(FakeDriver):
            def __init__(self, sid, proc, ident):
                self._proc = proc
                self._ident = ident
                super().__init__(sid)

            @property
            def is_running(self):
                return self._proc.poll() is None

            @is_running.setter
            def is_running(self, _value):
                pass

            def process_tree_target(self):
                return ProcessTreeTarget(
                    pid=self._ident.pid,
                    pgid=self._ident.pid,
                    start_ticks=self._ident.start_ticks,
                    poll=self._proc.poll,
                    provider="anthropic",
                    registry_path=reg,
                )

        d_live = RealFakeDriver("live", p_live, id_live)
        d_tearing = RealFakeDriver("tearing", p_tearing, id_tearing)
        mgr = DriverManager(max_live=4, idle_seconds=60)
        mgr.live = {"live": d_live}
        mgr._tearing_down.add(d_tearing)

        mgr.stop_all(interrupt=False, wait_timeout=0.2)

        assert d_live.stopped == [False]
        assert not is_process_tree_live(p_live.pid, p_live.pid, expected_start_ticks=id_live.start_ticks)
        assert not is_process_tree_live(p_tearing.pid, p_tearing.pid, expected_start_ticks=id_tearing.start_ticks)
        assert d_tearing not in mgr._tearing_down
    finally:
        for p in (p_live, p_tearing):
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def test_stop_all_reaps_forgotten_driver_surviving_descendant(tmp_path):
    """Finding 4: an independently exited/forgotten driver's surviving group is reaped."""
    import os
    import signal
    import subprocess
    import sys
    import pytest
    from helios.backend.process.child_registry import (
        CHILD_GROUP_ENV,
        is_process_tree_live,
        new_group_token,
        read_process_identity,
        register_child,
        unregister_if_dead,
    )

    if sys.platform != "linux":
        pytest.skip("Linux /proc process identity required")

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
        mgr = DriverManager(max_live=4, idle_seconds=60)
        d = FakeDriver("exited-chat")
        mgr.live = {"exited-chat": d}
        mgr.forget(d)
        assert d not in mgr.live.values()
        assert d not in mgr._tearing_down

        mgr.stop_all(interrupt=False, wait_timeout=0.3, registry_path=reg)

        assert not is_process_tree_live(
            ident.pid,
            ident.pid,
            expected_start_ticks=ident.start_ticks,
            expected_group_token=token,
        )
        gc_now = read_process_identity(gc_pid)
        assert gc_now is None or gc_now.state == "Z"
    finally:
        try:
            os.kill(gc_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.killpg(ident.pid, signal.SIGKILL)
        except OSError:
            pass
