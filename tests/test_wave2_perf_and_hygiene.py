"""Wave 2 stability tests: async/bounded checkpoints, WAL checkpointing,
injected WorkStore on SessionList, title/hidden-model pruning, cross-provider
leg transcript resolution, and SessionBrowser WebKit cleanup + LRU eviction.
"""

from __future__ import annotations

import json
import types

import pytest

from helios.backend import checkpoints, model_catalog, title_store, ui_state
from helios.backend.work_store import WorkStore


def test_checkpoints_async_write_forget_and_session_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(checkpoints, "MAX_SESSIONS", 3)

    for idx in range(5):
        cp = checkpoints.Checkpoint(
            tree=f"tree-{idx}",
            created_at=1_700_000_000 + idx * 10,
            label=f"msg {idx}",
            cwd=str(tmp_path),
        )
        checkpoints.record_async(f"sess-{idx}", cp)

    assert checkpoints.flush(timeout=5.0) is True
    store_file = tmp_path / "state" / "checkpoints.json"
    assert store_file.is_file()
    raw = store_file.read_text(encoding="utf-8")
    # Compact JSON (no multi-line pretty-print bloat).
    assert "\n" not in raw
    parsed = json.loads(raw)
    assert set(parsed.keys()) == {"sess-2", "sess-3", "sess-4"}
    assert checkpoints.load("sess-4")[0].tree == "tree-4"
    assert checkpoints.load("sess-0") == []

    checkpoints.forget_many_async(["sess-2", "sess-4"])
    assert checkpoints.flush(timeout=5.0) is True
    assert checkpoints.load("sess-2") == []
    assert checkpoints.load("sess-4") == []
    assert checkpoints.load("sess-3")[0].tree == "tree-3"


def test_work_store_checkpoint_wal_succeeds(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        store.create_work(objective="wal test", cwd=str(tmp_path))
        result = store.checkpoint_wal()
        assert result is not None
        busy, log_frames, checkpointed = result
        assert busy == 0
        assert log_frames >= 0
        assert checkpointed >= 0
    finally:
        store.close()


def test_title_store_delete_many_prunes_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    ts = title_store.TitleStore()
    ts.set("s1", "First")
    ts.set("s2", "Second")
    ts.set("s3", "Third")

    removed = ts.delete_many(["s1", "s3", "missing"])
    assert removed == 2
    assert ts.get("s1") is None
    assert ts.get("s2") == "Second"
    assert ts.get("s3") is None

    reloaded = title_store.TitleStore()
    assert reloaded.get("s1") is None
    assert reloaded.get("s2") == "Second"


def test_prune_hidden_models_only_drops_obsolete_entries_for_loaded_providers(tmp_path):
    state = ui_state.UiStateStore(tmp_path / "ui-state.json")
    state.set(
        ui_state.HIDDEN_MODELS_KEY,
        [
            "gpt-5.4",
            "gpt-4.1-obsolete",
            "gemini-3.8-flash",
            "gemini-1.0-dead",
            "muse-keep-because-unloaded",
        ],
    )

    removed = ui_state.prune_hidden_models(
        {
            model_catalog.PROVIDER_OPENAI: {"gpt-5.4", "gpt-5.5"},
            model_catalog.PROVIDER_GOOGLE: {"gemini-3.8-flash"},
            # Muse omitted (discovery failed / not loaded) -> must be preserved!
        },
        state=state,
    )
    assert removed == 2
    assert state.get(ui_state.HIDDEN_MODELS_KEY) == [
        "gpt-5.4",
        "gemini-3.8-flash",
        "muse-keep-because-unloaded",
    ]


def test_session_list_reuses_injected_work_store_without_reopening_db(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk

    if Gdk.Display.get_default() is None:
        pytest.skip("No GTK display available")

    from helios.backend import work_store as ws_mod
    from helios.widgets.session_list import SessionList

    store = WorkStore(tmp_path / "work.db")
    try:
        sl = SessionList(work_store=store)
        try:
            def _forbid_open(*_args, **_kwargs):
                raise AssertionError("SessionList must not open a fresh WorkStore when injected")

            monkeypatch.setattr(ws_mod, "WorkStore", _forbid_open)
            assert sl._load_continuity_groups() == {}
        finally:
            sl.shutdown()
    finally:
        store.close()


def test_outgoing_leg_identity_resolves_non_claude_transcript_path(tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    try:
        gi.require_version("GtkSource", "5")
    except ValueError:
        pytest.skip("GtkSource 5 unavailable")

    from helios.main_window import MainWindow

    codex_file = tmp_path / "rollout-2026-11-23-thr-abc.jsonl"
    codex_file.write_text("{}\n", encoding="utf-8")

    win = types.SimpleNamespace(
        _driver=None,
        _work_coordinator=None,
        _next_chat=types.SimpleNamespace(
            work_id="wrk_1",
            resume_id="thr-abc",
            project=types.SimpleNamespace(path=tmp_path / "claude_proj", cwd=str(tmp_path)),
        ),
        _sessions=types.SimpleNamespace(
            session_path=lambda sid: str(codex_file) if sid == "thr-abc" else ""
        ),
        _ctx_fill_session=None,
        _driver_provider=lambda _d: "openai",
    )
    # Bypass _target_resume_resolution by setting driver directly
    win._driver = types.SimpleNamespace(
        model="gpt-5.4",
        session_id="thr-abc",
        _helios_requested_model="gpt-5.4",
    )

    model, sid, tpath = MainWindow._outgoing_leg_identity(
        win, old_provider="openai", old_model="gpt-5.4"
    )
    assert model == "gpt-5.4"
    assert sid == "thr-abc"
    assert tpath == str(codex_file)


def test_session_browser_close_session_and_lru_eviction(tmp_path, monkeypatch, request):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")

    from helios.widgets.browser_pane import SessionBrowser

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.html").write_text("<title>A</title><p>A</p>", encoding="utf-8")

    browser = SessionBrowser(
        on_shown=lambda shown: browser.set_shown(shown),
        attach=lambda _path: None,
        cwd=lambda: str(proj),
    )
    if not browser.available:
        pytest.skip("WebKitGTK 6 is not installed")
    browser._MAX_HIDDEN_PANES = 2

    window = Gtk.Window()
    window.set_default_size(640, 480)
    window.set_child(browser)
    request.addfinalizer(lambda: (browser.shutdown(), window.destroy()))
    window.present()

    # Open 4 distinct sessions; cap is 2 hidden + 1 active = max 3 live panes.
    for idx in range(4):
        browser.select_session(f"s-{idx}", work_id=f"w-{idx}")
        browser.set_shown(True)

    assert len(browser._panes) == 3
    # Oldest scope (s-0) was evicted; current (s-3) and two most recent (s-1, s-2) remain.
    assert browser._scopes[("", "s-3")] in browser._panes
    assert browser._scopes[("", "s-2")] in browser._panes
    assert browser._scopes[("", "s-1")] in browser._panes
    assert browser._scopes[("", "s-0")] not in browser._panes

    # Explicitly closing an archived/deleted session destroys its pane immediately.
    assert browser.close_session("s-2", work_id="w-2") is True
    assert ("", "s-2") not in browser._scopes
    assert ("work", "w-2") not in browser._scopes
    assert len(browser._panes) == 2

    # Closing the active session hides the browser cleanly.
    assert browser.close_session("s-3", work_id="w-3") is True
    assert not browser.get_visible()
    assert len(browser._panes) == 1
