"""End-to-end replay tests for ProductBrief continuity, handoffs, and delivery evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helios.backend import attachments, codex_context, model_catalog, session_handoff, session_providers
from helios.backend.product_brief import (
    BriefAuthorityError,
    DeliveryEvidence,
    ExplicitStop,
    StaleBriefRevisionError,
)
from helios.backend.projects import Session
from helios.backend.session_goals import GoalState
from helios.backend.work_context import CollaborationUpdate, build_packet
from helios.backend.work_coordinator import WorkCoordinator
from helios.backend.work_store import SCHEMA_VERSION, WorkStore


# Minimal 1x1 PNG fixture bytes (public-safe synthetic image)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_PNG_ALT = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
    b"\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_session(
    tmp_path: Path,
    *,
    session_id: str,
    provider: str,
    cwd: str = "/workspace/sprout",
    turns: list[tuple[str, str]] | None = None,
) -> Session:
    import json
    from helios.backend import projects as projects_mod

    state = tmp_path / "state"
    os.environ["HELIOS_STATE_DIR"] = str(state)
    session_providers.reload()

    proj_dir = tmp_path / "projects" / f"proj-{session_id}"
    proj_dir.mkdir(parents=True, exist_ok=True)
    tpath = proj_dir / f"{session_id}.jsonl"
    version_map = {
        "anthropic": "1.0.0",
        "openai": "helios-codex",
        "google": "helios-gemini",
        "muse": "helios-muse",
        "kimi": "helios-kimi",
        "openrouter": "helios-openrouter",
    }
    rec_lines = []
    for role, msg_text in turns or [("user", "Adjust Sprout sidebar layout")]:
        rec_lines.append(
            json.dumps(
                {
                    "type": role,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "version": version_map.get(provider, "1.0.0"),
                    "message": {"role": role, "content": msg_text},
                }
            )
        )
    tpath.write_text("\n".join(rec_lines) + "\n", encoding="utf-8")
    proj = projects_mod._scan_project_dir(proj_dir, origin="local", read_only=False)
    assert proj is not None
    sess = proj.load_sessions()[0]
    session_providers.set_provider(sess.session_id, provider)
    return sess


def test_steered_objective_reconciles_cleanly_and_retains_revision_history(tmp_path: Path) -> None:
    """Scenario 1: Steered objective updates current brief and records superseded framing."""
    store = WorkStore(tmp_path / "works.db")
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    work = store.create_work(
        objective="Expose Backgrounds and Icons as two separate sidebar panels in Sprout Settings",
        definition_of_done="Both panels navigate cleanly with native Settings typography",
        cwd="/workspace/sprout",
    )
    initial = store.get_product_brief(work.work_id)
    assert initial.revision == 0
    assert "two separate sidebar panels" in initial.outcome

    updated = store.update_product_brief(
        work.work_id,
        expected_revision=0,
        author_role="user",
        outcome="Keep Backgrounds integrated at the top of Appearance with shared scrolling; keep Icons as its own sidebar panel",
        criteria=[
            {
                "criterion_id": "APP-01",
                "text": "Backgrounds, Accent Color, and Style scroll together at short window height",
            },
            {
                "criterion_id": "APP-02",
                "text": "Display previews wrap cleanly without nested window controls",
            },
        ],
        rejected_approaches=["Standalone Backgrounds top-level panel"],
        summary="User steered Backgrounds into top of Appearance",
    )
    assert updated.revision == 1
    assert "two separate sidebar panels" in updated.supersedes
    assert store.get_work(work.work_id).objective == updated.outcome

    revisions = store.list_product_brief_revisions(work.work_id)
    assert len(revisions) == 1
    assert revisions[0].revision == 1
    assert revisions[0].supersedes == work.objective


def test_annotated_reference_images_and_long_correction_survive_deleted_tmp_and_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 2: Reference screenshots + >700-char correction survive /tmp deletion and handoff."""
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    session_providers.reload()
    store = WorkStore(tmp_path / "state" / "works" / "works.db")
    coord = WorkCoordinator(store)

    work = coord.ensure_work(
        cwd="/workspace/sprout",
        lead_provider=model_catalog.PROVIDER_MUSE,
        objective="Polish Sprout Mail three-pane layout",
        definition_of_done="Active/inactive sidebar transitions match target design",
    )
    coord.bind_participant(
        work.work_id,
        model_catalog.PROVIDER_MUSE,
        native_id="sess-sprout-mail-01",
    )

    tmp_shot_1 = tmp_path / "tmp_sidebar_bug.png"
    tmp_shot_2 = tmp_path / "tmp_target_divider.png"
    tmp_shot_1.write_bytes(_TINY_PNG)
    tmp_shot_2.write_bytes(_TINY_PNG_ALT)

    ref1 = attachments.persist_reference_attachment(
        store,
        work.work_id,
        tmp_shot_1,
        role="bug_annotation",
        caption="Active sidebar splits into two grey shades when search entry focuses",
        source_message_id="msg-101",
    )
    ref2 = attachments.persist_reference_attachment(
        store,
        work.work_id,
        tmp_shot_2,
        role="target",
        caption="Thin 1px pane divider with persistent glass message list",
        source_message_id="msg-102",
    )

    # Duplicate registration of identical bytes must be idempotent, never error
    ref1_dup = attachments.persist_reference_attachment(
        store,
        work.work_id,
        tmp_shot_1,
        role="bug_annotation",
        caption="Active sidebar splits into two grey shades when search entry focuses",
        source_message_id="msg-101",
    )
    assert ref1_dup.artifact_id == ref1.artifact_id

    long_correction = (
        "Keep the left folder sidebar as one unified translucent-grey surface whenever the "
        "Sprout Mail window is active, and transition to window glass only when the toplevel "
        "window enters :backdrop. Focusing the search entry or dragging the window across "
        "monitors must never flicker the sidebar material. Keep the middle message list glass, "
        "match the 1px vertical divider weight on both pane boundaries, let the search bar "
        "resize smoothly with the header, and flatten capsule buttons in the conversation "
        "toolbar while preserving unified inbox account filtering across restarts. "
        "Also ensure inactive folders, drafts, sent, and archive rows never inherit separate " "opaque fills, preserve keyboard focus rings inside the search field without altering " "the parent sidebar backdrop class, and verify light/dark surface tokens remain balanced. " "MARKER_TAIL_BYTE_820_MUST_SURVIVE_UNTRUNCATED."
    )
    assert len(long_correction) > 700
    store.record_user_correction(
        work.work_id,
        long_correction,
        artifact_ids=[ref1.artifact_id, ref2.artifact_id],
    )

    # Delete original temporary files to simulate /tmp cleanup before takeover
    tmp_shot_1.unlink()
    tmp_shot_2.unlink()
    assert not tmp_shot_1.exists()

    # Provider attachment resolution still recovers durable bytes from Work artifacts!
    prompt_with_deleted_path = f"Fix the divider in @{tmp_shot_1} and @{tmp_shot_2}"
    report = attachments.resolve_for_provider(
        model_catalog.PROVIDER_ANTHROPIC,
        prompt_with_deleted_path,
        store=store,
        work_id=work.work_id,
        include_brief_references=True,
    )
    assert len(report.wire_items) == 2
    assert all(item.disposition == "image_prepared" for item in report.items)

    # Handoff payload places product_brief ahead of 700-char truncated transcript_tail
    session = _make_session(
        tmp_path,
        session_id="sess-sprout-mail-01",
        provider=model_catalog.PROVIDER_MUSE,
        turns=[("user", long_correction), ("assistant", "Investigating CSS selectors.")],
    )
    payload = session_handoff.build_payload(
        session,
        "Sprout Mail Sidebar Handoff",
        recipient_provider=model_catalog.PROVIDER_OPENAI,
        work_store=store,
    )
    keys = list(payload.keys())
    assert keys.index("product_brief") < keys.index("transcript_tail")
    assert payload["work_id"] == work.work_id
    assert payload["intent_revision"] >= 2
    pb = payload["product_brief"]
    assert "MARKER_TAIL_BYTE_820_MUST_SURVIVE_UNTRUNCATED" in pb["unresolved_corrections"][0]["text"]
    assert len(pb["reference_artifacts"]) == 2
    assert Path(pb["reference_artifacts"][0]["durable_path"]).is_file()


def test_packet_budget_preserves_essential_brief_and_emits_omission_notice(tmp_path: Path) -> None:
    """Scenario 3: Tight packet budget retains outcome, stop, correction, and refs while truncating chatter."""
    store = WorkStore(tmp_path / "works.db")
    work = store.create_work(
        objective="Deliver native Linux spreadsheet toolbar study",
        definition_of_done="Verified on native Linux Wayland session",
        cwd="/workspace/sprout",
    )
    shot = tmp_path / "approved_study.png"
    shot.write_bytes(_TINY_PNG)
    ref = attachments.persist_reference_attachment(
        store, work.work_id, shot, role="approved_example", caption="Approved Linux study"
    )
    store.update_product_brief(
        work.work_id,
        author_role="user",
        explicit_stops=[
            ExplicitStop(
                stop_id="stop-cross-os-demo",
                scope="demo_and_build",
                reason="Stop cross-OS demo trials; do not resume until user clears approach",
            )
        ],
    )
    store.record_user_correction(
        work.work_id,
        "Only native Linux Wayland rendering matches the approved toolbar geometry.",
        artifact_ids=[ref.artifact_id],
    )
    brief = store.get_product_brief(work.work_id)
    essential = brief.render_essential_context(
        artifact_paths={ref.artifact_id: str(store.get_artifact(work.work_id, ref.artifact_id).path)}
    )

    chatter = [
        CollaborationUpdate(
            seq=i,
            kind="participant.contribution",
            provider="openai",
            text=f"Verbose intermediate build log #{i}: " + ("x" * 600),
        )
        for i in range(1, 20)
    ]
    packet = build_packet(
        work.work_id,
        chatter,
        essential_intent=essential,
        accepted_state="Status: active",
        max_chars=2_800,
        max_updates=24,
    )
    assert "ACTIVE STOP [stop-cross-os-demo]" in packet.text
    assert "Only native Linux Wayland rendering matches" in packet.text
    assert ref.artifact_id in packet.text
    assert packet.truncated is True
    assert packet.omitted_update_count > 0
    assert "omitted to preserve brief budget" in packet.text
    assert packet.through_seq < 19


def test_failed_prompt_delivery_does_not_advance_participant_cursor(tmp_path: Path) -> None:
    """Scenario 4: Unacknowledged PreparedPrompt leaves participant cursor unchanged."""
    store = WorkStore(tmp_path / "works.db")
    coord = WorkCoordinator(store)
    work = coord.ensure_work(
        cwd="/workspace/sprout",
        lead_provider=model_catalog.PROVIDER_ANTHROPIC,
        objective="Implement Sprout dock hover indicator",
        definition_of_done="Hover pill matches menu material",
        mode="tandem",
    )
    p_claude = coord.bind_participant(work.work_id, model_catalog.PROVIDER_ANTHROPIC, native_id="c-1")
    p_gpt = coord.bind_participant(work.work_id, model_catalog.PROVIDER_OPENAI, native_id="g-1")

    store.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "Updated dock CSS token geometry."},
        provider=model_catalog.PROVIDER_ANTHROPIC,
        emitting_participant_id=p_claude.participant_id,
        expected_participant_generation=p_claude.generation,
    )
    before_seq = store.participant_for_provider(work.work_id, model_catalog.PROVIDER_OPENAI).last_event_seq
    prepared = coord.prepare_prompt(
        work_id=work.work_id,
        provider=model_catalog.PROVIDER_OPENAI,
        text="Review the dock hover token changes",
        goal=GoalState(objective=work.objective, definition_of_done=work.definition_of_done),
    )
    assert hasattr(prepared, "mark_sent")
    assert getattr(prepared, "diagnostics", None) is not None
    assert prepared.diagnostics.provider == model_catalog.PROVIDER_OPENAI

    # Simulate transport failure before provider accepts turn: do NOT call mark_sent()
    # The contribution event seq remains unacknowledged so retry still receives it.
    del before_seq
    after_fail_seq = store.participant_for_provider(work.work_id, model_catalog.PROVIDER_OPENAI).last_event_seq
    retry_prepared = coord.prepare_prompt(
        work_id=work.work_id,
        provider=model_catalog.PROVIDER_OPENAI,
        text="Review the dock hover token changes",
        goal=GoalState(objective=work.objective, definition_of_done=work.definition_of_done),
    )
    assert "Updated dock CSS token geometry." in str(retry_prepared)
    assert store.participant_for_provider(work.work_id, model_catalog.PROVIDER_OPENAI).last_event_seq == after_fail_seq

    # Retry succeeds and calls mark_sent() -> cursor advances once
    prepared.mark_sent()
    after_ok_seq = store.participant_for_provider(work.work_id, model_catalog.PROVIDER_OPENAI).last_event_seq
    assert after_ok_seq > p_gpt.last_event_seq


def test_revision_conflict_and_authority_guards_protect_stops_and_acceptance(tmp_path: Path) -> None:
    """Scenarios 5 & 6: Stale revision fails; model cannot clear user stop or self-accept."""
    store = WorkStore(tmp_path / "works.db")
    work = store.create_work(
        objective="Prototype Sprout Calc toolbar",
        definition_of_done="Approved on Linux desktop",
        cwd="/workspace/sprout",
    )
    b1 = store.update_product_brief(
        work.work_id,
        expected_revision=0,
        author_role="user",
        criteria=[{"criterion_id": "CALC-1", "text": "Toolbar matches approved Linux study"}],
        explicit_stops=[
            {
                "stop_id": "stop-calc-build",
                "scope": "build_and_demo",
                "reason": "User paused current build/demo approach",
            }
        ],
    )
    assert b1.revision == 1

    # Stale expected_revision is rejected
    with pytest.raises(StaleBriefRevisionError):
        store.update_product_brief(
            work.work_id,
            expected_revision=0,
            author_role="user",
            outcome="Stale overwrite attempt",
        )

    # Model cannot clear active explicit stop
    with pytest.raises(BriefAuthorityError):
        store.update_product_brief(
            work.work_id,
            expected_revision=b1.revision,
            author_role="model",
            explicit_stops=[],
        )

    # Model cannot mark criterion user_accepted=True
    with pytest.raises(BriefAuthorityError):
        store.update_product_brief(
            work.work_id,
            expected_revision=b1.revision,
            author_role="model",
            criteria=[
                {
                    "criterion_id": "CALC-1",
                    "text": "Toolbar matches approved Linux study",
                    "user_accepted": True,
                }
            ],
        )

    # Subsequent model note update that preserves stops succeeds and keeps stop active
    b2 = store.update_product_brief(
        work.work_id,
        expected_revision=b1.revision,
        author_role="model",
        source_message_ids=["msg-200"],
    )
    assert len(b2.active_stops()) == 1
    assert b2.active_stops()[0].stop_id == "stop-calc-build"


def test_delivery_evidence_caps_stage_when_loaded_process_is_stale_or_shadowed(tmp_path: Path) -> None:
    """Scenario 7: Staged package with stale singleton stays below loaded_normal_launcher/user_accepted."""
    store = WorkStore(tmp_path / "works.db")
    work = store.create_work(
        objective="Install Sprout Control Center package",
        definition_of_done="Normal launcher loads updated binary",
        cwd="/workspace/sprout",
    )
    store.update_product_brief(
        work.work_id,
        author_role="user",
        criteria=[{"criterion_id": "PKG-1", "text": "Desktop launcher opens updated binary"}],
    )

    # Model attempts to claim loaded_normal_launcher while stale_process_detected=True
    b_mismatch = store.record_delivery_evidence(
        work.work_id,
        DeliveryEvidence(
            evidence_id="ev-stale",
            criterion_id="PKG-1",
            candidate_sha="abc1234",
            installed_artifact="/usr/bin/sprout-settings@sha256:new",
            loaded_process="/usr/bin/sprout-settings@sha256:old",
            normal_entry_point="/usr/share/applications/sprout-settings.desktop",
            functional_status="verified",
            visual_status="candidate",
            readiness_stage="loaded_normal_launcher",
            stale_process_detected=True,
        ),
        author_role="model",
    )
    crit = b_mismatch.criteria[0]
    assert crit.readiness_stage == "installed_candidate"
    assert crit.user_accepted is False

    # Model cannot self-certify user_accepted on DeliveryEvidence
    with pytest.raises(BriefAuthorityError):
        store.record_delivery_evidence(
            work.work_id,
            {
                "criterion_id": "PKG-1",
                "readiness_stage": "user_accepted",
                "user_accepted": True,
            },
            author_role="model",
        )

    # Clean loaded process reaches loaded_normal_launcher, then user records acceptance
    b_clean = store.record_delivery_evidence(
        work.work_id,
        {
            "evidence_id": "ev-clean",
            "criterion_id": "PKG-1",
            "candidate_sha": "abc1234",
            "installed_artifact": "/usr/bin/sprout-settings@sha256:new",
            "loaded_process": "/usr/bin/sprout-settings@sha256:new",
            "normal_entry_point": "/usr/share/applications/sprout-settings.desktop",
            "functional_status": "verified",
            "visual_status": "verified",
            "readiness_stage": "loaded_normal_launcher",
        },
        author_role="model",
    )
    assert b_clean.criteria[0].readiness_stage == "loaded_normal_launcher"
    assert b_clean.criteria[0].user_accepted is False

    b_accepted = store.record_user_acceptance(work.work_id, "PKG-1", accepted=True)
    assert b_accepted.criteria[0].user_accepted is True
    assert b_accepted.criteria[0].readiness_stage == "user_accepted"

    # A new user correction targeting PKG-1 resets stale user_accepted / visual claim
    b_reopened = store.record_user_correction(
        work.work_id,
        "Narrow window width still clips the rightmost monitor card.",
        criterion_ids=["PKG-1"],
    )
    assert b_reopened.criteria[0].user_accepted is False
    assert b_reopened.criteria[0].visual_status == "pending"


def test_all_six_providers_handoff_resume_dispositions_and_live_prompt_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 8: All 6 providers produce valid handoff summaries, dispositions, and live prompt refresh."""
    expected_cli = {
        model_catalog.PROVIDER_ANTHROPIC: "claude --resume",
        model_catalog.PROVIDER_OPENAI: "codex exec resume",
        model_catalog.PROVIDER_MUSE: "muse resume",
        model_catalog.PROVIDER_KIMI: "kimi --session",
        model_catalog.PROVIDER_GOOGLE: "reopen this session in Helios",
        model_catalog.PROVIDER_OPENROUTER: "reopen this session in Helios",
    }
    shot = tmp_path / "shot.png"
    shot.write_bytes(_TINY_PNG)

    for idx, (provider, marker) in enumerate(expected_cli.items()):
        sess = _make_session(
            tmp_path,
            session_id=f"sess-{provider}-{idx}",
            provider=provider,
        )
        summary = session_handoff.default_summary(sess, f"Title {provider}")
        assert marker in summary
        payload = session_handoff.build_payload(sess, f"Title {provider}")
        assert marker in payload["resume"]

        report = attachments.resolve_for_provider(provider, f"See @{shot}")
        if provider == model_catalog.PROVIDER_GOOGLE:
            assert report.items[0].disposition == "text_only_reference"
        else:
            assert report.items[0].disposition == "image_prepared"
            assert len(report.wire_items) == 1

    # Live refresh of system-prompt.d without process restart
    cfg = tmp_path / "xdg_cfg"
    prompt_dir = cfg / "helios" / "system-prompt.d"
    prompt_dir.mkdir(parents=True)
    prev_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(cfg)
    try:
        (prompt_dir / "10-style.md").write_text("Use single-node window translucency.", encoding="utf-8")
        refreshed_1 = codex_context.refresh_extra_system_prompt()
        digest_1 = codex_context.instruction_prompt_digest("openai")
        assert "single-node window translucency" in refreshed_1
        assert "single-node window translucency" in codex_context.CODEX_DEVELOPER_INSTRUCTIONS

        (prompt_dir / "20-verify.md").write_text("Verify normal desktop launcher.", encoding="utf-8")
        refreshed_2 = codex_context.refresh_extra_system_prompt()
        digest_2 = codex_context.instruction_prompt_digest("openai")
        assert "Verify normal desktop launcher." in refreshed_2
        assert digest_2 != digest_1
    finally:
        if prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = prev_xdg
        codex_context.refresh_extra_system_prompt()


def test_gtk_brief_widgets_render_signals_under_xvfb(tmp_path: Path) -> None:
    """H5: GoalStrip and SharedContextPane surface brief revision, stops, corrections, and references."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no GTK display")
    from helios.widgets.goal_strip import GoalStrip
    from helios.widgets.shared_context_pane import SharedContextPane

    store = WorkStore(tmp_path / "works.db")
    work = store.create_work(
        objective="Integrate Sprout Wallpaper inside Appearance",
        definition_of_done="Shared vertical scroll and wrapping 224px cards",
        cwd="/workspace/sprout",
    )
    shot = tmp_path / "ref.png"
    shot.write_bytes(_TINY_PNG)
    ref = attachments.persist_reference_attachment(
        store, work.work_id, shot, role="target", caption="224px card grid"
    )
    store.record_user_correction(
        work.work_id,
        "Remove nested vertical scroller above wallpaper section.",
        artifact_ids=[ref.artifact_id],
    )
    brief = store.update_product_brief(
        work.work_id,
        explicit_stops=[
            {
                "stop_id": "stop-office",
                "scope": "Sprout Office demo",
                "reason": "Pause demo launches until toolbar review",
                "active": True,
            }
        ],
        author_role="user",
    )

    strip = GoalStrip()
    strip.set_goal(GoalState(objective=work.objective, definition_of_done=work.definition_of_done))
    strip.set_brief(brief)
    assert strip._brief_badge.get_visible() is True
    badge_text = strip._brief_badge.get_label()
    assert "Stop held (1)" in badge_text
    assert "1 open" in badge_text
    assert "1 ref" in badge_text
    assert f"Brief r{brief.revision}" in (strip._brief_badge.get_tooltip_text() or "")

    pane = SharedContextPane()
    pane.set_product_brief(brief)
    assert pane._brief_expander.get_visible() is True
    assert f"r{brief.revision}" in (pane._brief_expander.get_label() or "")
