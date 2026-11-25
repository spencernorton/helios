"""Which models the picker lists up front, and how hide/add/reorder persist.

GTK-free: helios.backend.model_picker owns the rules; the toolbar only draws
them. Each test uses its own ui-state file.
"""

from __future__ import annotations

import pytest

from helios.backend import model_catalog, model_picker, ui_state
from helios.backend.model_catalog import ModelEntry

ANTHROPIC = model_catalog.PROVIDER_ANTHROPIC
OPENAI = model_catalog.PROVIDER_OPENAI
OPENROUTER = model_catalog.PROVIDER_OPENROUTER


def claude() -> list[ModelEntry]:
    """The shape a binary scan produces today (see build_anthropic_entries)."""
    return [
        ModelEntry("fable[1m]", "Fable 5.2", "Latest"),
        ModelEntry("opus[1m]", "Opus 5.5", "Latest"),
        ModelEntry("sonnet[1m]", "Sonnet 5.5", "Latest"),
        ModelEntry("haiku", "Haiku 5.5", "Latest"),
        model_catalog.CLAUDE_CODE_DEFAULT,
        ModelEntry("claude-fable-5-2", "Fable 5.2", "Fable"),
        ModelEntry("claude-fable-5-1", "Fable 5.1", "Fable"),
        ModelEntry("claude-opus-5-5", "Opus 5.5", "Opus"),
        ModelEntry("claude-opus-4-8", "Opus 4.8", "Opus"),
        ModelEntry("claude-sonnet-5-5", "Sonnet 5.5", "Sonnet"),
        ModelEntry("claude-haiku-5-5", "Haiku 5.5", "Haiku"),
        ModelEntry("claude-haiku-4-5-20251001", "Haiku 4.5 (2025-10-01)", "Haiku"),
    ]


def gpt(n: int = 3) -> list[ModelEntry]:
    return [
        ModelEntry(f"gpt-6.{n - i}", f"GPT-6.{n - i}", "OpenAI", OPENAI)
        for i in range(n)
    ]


def openrouter() -> list[ModelEntry]:
    return [
        ModelEntry("deepseek/deepseek-v4", "DeepSeek V4", "DeepSeek", OPENROUTER),
        ModelEntry("moonshotai/kimi-k3", "Kimi K3", "Moonshot", OPENROUTER),
        ModelEntry("z-ai/glm-5.2", "GLM 5.2", "Z.AI", OPENROUTER),
    ]


@pytest.fixture
def state(tmp_path) -> ui_state.UiStateStore:
    return ui_state.UiStateStore(tmp_path / "ui-state.json")


def section(entries, state, provider=ANTHROPIC, selected=""):
    found = [
        s for s in model_picker.stored_sections(entries, selected=selected, state=state)
        if s.provider == provider
    ]
    return found[0] if found else None


def ids(rows) -> list[str]:
    return [e.id for e in rows]


# ── What shows where ────────────────────────────────────────────────────────


def test_claude_list_is_one_row_per_family_and_the_rest_is_more(state):
    s = section(claude(), state)
    assert ids(s.main) == ["fable[1m]", "opus[1m]", "sonnet[1m]", "haiku"]
    # Pins that name the alias rows' model (Fable 5.2, Opus 5.5…) are twins and
    # never listed; the Claude Code default closes the More list.
    assert ids(s.more) == [
        "claude-fable-5-1",
        "claude-opus-4-8",
        "claude-haiku-4-5-20251001",
        "",
    ]
    assert not s.customized


def test_there_is_no_other_group_and_the_sentinel_says_what_it_is():
    assert model_catalog.CLAUDE_CODE_DEFAULT.id == ""
    assert model_catalog.CLAUDE_CODE_DEFAULT.label == "Claude Code default"
    assert "--model" in model_catalog.CLAUDE_CODE_DEFAULT.description
    assert all(e.group != "Other" for e in model_catalog.FALLBACK_ANTHROPIC)


def test_sections_follow_provider_order_and_never_mix(state):
    entries = openrouter() + gpt() + claude()
    sections = model_picker.stored_sections(entries, state=state)
    assert [s.provider for s in sections] == [ANTHROPIC, OPENAI, OPENROUTER]
    for s in sections:
        assert {e.provider for e in s.main + s.more} == {s.provider}
    assert [s.title for s in sections] == ["Claude", "GPT", "OpenRouter"]


def test_a_selected_twin_is_listed_under_more(state):
    s = section(claude(), state, selected="claude-opus-5-5")
    assert "claude-opus-5-5" in ids(s.more)


def test_short_native_catalogs_show_everything_and_long_ones_cap(state):
    assert ids(section(gpt(3), state, OPENAI).main) == ["gpt-6.3", "gpt-6.2", "gpt-6.1"]
    long = section(gpt(11), state, OPENAI)
    assert len(long.main) == model_picker.DEFAULT_LIST_CAP
    assert len(long.more) == 11 - model_picker.DEFAULT_LIST_CAP


def test_openrouter_list_is_the_settings_shortlist(state):
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["z-ai/glm-5.2"])
    s = section(openrouter(), state, OPENROUTER)
    assert ids(s.main) == ["z-ai/glm-5.2"]
    assert ids(s.more) == ["deepseek/deepseek-v4", "moonshotai/kimi-k3"]


def test_unconfigured_openrouter_keeps_its_catalog_under_more(state):
    # Several hundred rows up front is the clutter this picker exists to
    # avoid; the "More OpenRouter models (N)" count says they are there.
    s = section(openrouter(), state, OPENROUTER)
    assert s.main == ()
    assert ids(s.more) == ids(openrouter())


def test_no_alias_rows_falls_back_to_the_newest_pin_per_family(state):
    entries = [e for e in claude() if e.group != "Latest"]
    s = section(entries, state)
    assert ids(s.main) == [
        "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    ]


def test_search_predicate_covers_id_label_group_and_provider():
    entry = ModelEntry("moonshotai/kimi-k3", "Kimi K3", "Moonshot", OPENROUTER)
    for needle in ("moonshotai", "k3", "MOONSHOT", "openrouter", "  "):
        assert model_picker.matches(entry, needle)
    assert not model_picker.matches(entry, "sonnet")


# ── Curation ────────────────────────────────────────────────────────────────


def test_hiding_a_family_moves_it_to_more_and_keeps_the_rest_in_order(state):
    model_picker.hide_model(section(claude(), state), "haiku", state=state)
    s = section(claude(), state)
    assert ids(s.main) == ["fable[1m]", "opus[1m]", "sonnet[1m]"]
    assert ids(s.more)[0] == "haiku"
    assert s.customized
    assert state.get(ui_state.HIDDEN_MODELS_KEY) == ["haiku"]


def test_adding_an_older_version_appends_it_to_the_list(state):
    model_picker.add_model(section(claude(), state), "claude-opus-4-8", state=state)
    s = section(claude(), state)
    assert ids(s.main) == [
        "fable[1m]", "opus[1m]", "sonnet[1m]", "haiku", "claude-opus-4-8",
    ]
    assert "claude-opus-4-8" not in ids(s.more)


def test_adding_a_hidden_default_unhides_it(state):
    state.set(ui_state.HIDDEN_MODELS_KEY, ["haiku"])
    model_picker.add_model(section(claude(), state), "haiku", state=state)
    assert state.get(ui_state.HIDDEN_MODELS_KEY) == []
    assert ids(section(claude(), state).main)[-1] == "haiku"


def test_hiding_an_added_model_just_takes_it_back_out(state):
    model_picker.add_model(section(claude(), state), "", state=state)
    assert ids(section(claude(), state).main)[-1] == ""
    model_picker.hide_model(section(claude(), state), "", state=state)
    # Not a default, so nothing to remember: it simply leaves the order.
    assert state.get(ui_state.HIDDEN_MODELS_KEY) == []
    assert "" not in ids(section(claude(), state).main)
    assert ids(section(claude(), state).more)[-1] == ""


def test_moving_reorders_one_provider_only(state):
    entries = claude() + gpt()
    model_picker.add_model(section(entries, state, OPENAI), "gpt-6.1", state=state)
    assert model_picker.move_model(section(entries, state), "haiku", 0, state=state)
    assert ids(section(entries, state).main) == [
        "haiku", "fable[1m]", "opus[1m]", "sonnet[1m]",
    ]
    assert ids(section(entries, state, OPENAI).main) == ["gpt-6.3", "gpt-6.2", "gpt-6.1"]
    # Out-of-range targets clamp; a no-op move reports False.
    assert not model_picker.move_model(section(entries, state), "haiku", -5, state=state)
    assert model_picker.move_model(section(entries, state), "haiku", 99, state=state)
    assert ids(section(entries, state).main)[-1] == "haiku"


def test_a_new_default_after_curation_joins_the_end_of_the_list(state):
    model_picker.move_model(section(claude(), state), "haiku", 0, state=state)
    newer = claude() + [ModelEntry("mythos[1m]", "Mythos 5", "Latest")]
    assert ids(section(newer, state).main) == [
        "haiku", "fable[1m]", "opus[1m]", "sonnet[1m]", "mythos[1m]",
    ]


def test_curating_a_partial_catalog_keeps_placements_it_cannot_see(state):
    model_picker.add_model(section(claude(), state), "claude-opus-4-8", state=state)
    # Only the static fallback loaded (no 4.8 row): hiding Haiku there must
    # not forget the 4.8 placement…
    fallback = [e for e in claude() if e.id != "claude-opus-4-8"]
    model_picker.hide_model(section(fallback, state), "haiku", state=state)
    assert "claude-opus-4-8" in state.get(ui_state.MODEL_ORDER_KEY)
    # …so it is back in the list when the full catalog returns.
    assert "claude-opus-4-8" in ids(section(claude(), state).main)
    assert "haiku" not in ids(section(claude(), state).main)


def test_reset_forgets_one_provider(state):
    entries = claude() + gpt()
    model_picker.hide_model(section(entries, state), "haiku", state=state)
    model_picker.hide_model(section(entries, state, OPENAI), "gpt-6.3", state=state)
    model_picker.reset_provider(ANTHROPIC, state=state)
    assert ids(section(entries, state).main) == [
        "fable[1m]", "opus[1m]", "sonnet[1m]", "haiku",
    ]
    assert not section(entries, state).customized
    assert "gpt-6.3" not in ids(section(entries, state, OPENAI).main)
    model_picker.reset_all(state=state)
    assert model_picker.customization_counts(state=state) == (0, 0)


def test_openrouter_edits_keep_the_settings_shortlist_in_step(state):
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["z-ai/glm-5.2"])
    model_picker.add_model(
        section(openrouter(), state, OPENROUTER), "moonshotai/kimi-k3", state=state,
    )
    assert state.get(ui_state.OPENROUTER_PICKER_KEY) == [
        "moonshotai/kimi-k3", "z-ai/glm-5.2",
    ]
    model_picker.hide_model(section(openrouter(), state, OPENROUTER), "z-ai/glm-5.2", state=state)
    model_picker.hide_model(
        section(openrouter(), state, OPENROUTER), "moonshotai/kimi-k3", state=state,
    )
    assert state.get(ui_state.OPENROUTER_PICKER_KEY) == []
    s = section(openrouter(), state, OPENROUTER)
    assert s.main == ()
    assert len(s.more) == 3


def test_settings_shortlist_edits_override_picker_curation(state):
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["z-ai/glm-5.2", "deepseek/deepseek-v4"])
    model_picker.hide_model(
        section(openrouter(), state, OPENROUTER), "deepseek/deepseek-v4", state=state,
    )
    # Settings ticks it again: it must show despite the earlier hide…
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["z-ai/glm-5.2", "deepseek/deepseek-v4"])
    model_picker.sync_openrouter_shortlist(
        {"z-ai/glm-5.2"}, {"z-ai/glm-5.2", "deepseek/deepseek-v4"}, state=state,
    )
    assert "deepseek/deepseek-v4" in ids(section(openrouter(), state, OPENROUTER).main)
    # …and unticking leaves the list even though it sits in the saved order.
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["deepseek/deepseek-v4"])
    model_picker.sync_openrouter_shortlist(
        {"z-ai/glm-5.2", "deepseek/deepseek-v4"}, {"deepseek/deepseek-v4"}, state=state,
    )
    assert ids(section(openrouter(), state, OPENROUTER).main) == ["deepseek/deepseek-v4"]
    # Clearing the shortlist clears OpenRouter's curation with it.
    model_picker.add_model(section(openrouter(), state, OPENROUTER), "z-ai/glm-5.2", state=state)
    state.set(ui_state.OPENROUTER_PICKER_KEY, [])
    model_picker.sync_openrouter_shortlist(
        {"deepseek/deepseek-v4", "z-ai/glm-5.2"}, set(), state=state,
    )
    s = section(openrouter(), state, OPENROUTER)
    assert s.main == ()
    assert not s.customized


def native_catalogs() -> list[ModelEntry]:
    """Id shapes the Gemini, Muse and Kimi CLIs really report."""
    google, muse, kimi = (
        model_catalog.PROVIDER_GOOGLE, model_catalog.PROVIDER_MUSE, model_catalog.PROVIDER_KIMI,
    )
    return [
        ModelEntry("gemini-4-neon", "Gemini 4 Neon", "Google", google),
        ModelEntry("gemini-pro-orchestrator", "Orchestrator (Carbon)", "Google", google),
        ModelEntry("gemini-3.8-flash", "Gemini 3.8 Flash", "Google", google),
        ModelEntry("muse-wonder-1.0", "muse-wonder-1.0", "Muse", muse),
        ModelEntry("muse-spark-1.3", "muse-spark-1.3", "Muse", muse),
        # Kimi ids carry a slash, like OpenRouter's vendor/model ids.
        ModelEntry("kimi-code/kimi-for-coding", "K2.8 Preview", "Kimi", kimi),
        ModelEntry("kimi-code/k3.1-pro", "K3.1 Pro", "Kimi", kimi),
    ]


@pytest.mark.parametrize("provider, first, second", [
    (model_catalog.PROVIDER_GOOGLE, "gemini-4-neon", "gemini-pro-orchestrator"),
    (model_catalog.PROVIDER_MUSE, "muse-wonder-1.0", "muse-spark-1.3"),
    (model_catalog.PROVIDER_KIMI, "kimi-code/kimi-for-coding", "kimi-code/k3.1-pro"),
])
def test_gemini_muse_and_kimi_lists_curate_like_any_other(state, provider, first, second):
    entries = native_catalogs() + openrouter() + claude()
    state.set(ui_state.OPENROUTER_PICKER_KEY, ["z-ai/glm-5.2"])
    before = {
        s.provider: s.main_ids
        for s in model_picker.stored_sections(entries, state=state)
    }
    # The whole catalog is listed until something is hidden.
    whole = [e.id for e in native_catalogs() if e.provider == provider]
    assert before[provider] == whole
    assert section(entries, state, provider).more == ()

    model_picker.hide_model(section(entries, state, provider), second, state=state)
    s = section(entries, state, provider)
    assert second not in s.main_ids
    assert ids(s.more) == [second]

    model_picker.add_model(section(entries, state, provider), second, state=state)
    model_picker.move_model(section(entries, state, provider), second, 0, state=state)
    assert section(entries, state, provider).main_ids[:2] == [second, first]

    # No other provider's list moved (Kimi's slash ids are not OpenRouter's).
    after = {
        s.provider: s.main_ids
        for s in model_picker.stored_sections(entries, state=state)
    }
    assert {p: v for p, v in after.items() if p != provider} == {
        p: v for p, v in before.items() if p != provider
    }
    assert state.get(ui_state.OPENROUTER_PICKER_KEY) == ["z-ai/glm-5.2"]

    model_picker.reset_provider(provider, state=state)
    assert section(entries, state, provider).main_ids == whole


def test_hand_edited_state_is_tolerated(state):
    state.set(ui_state.HIDDEN_MODELS_KEY, "haiku")
    state.set(ui_state.MODEL_ORDER_KEY, ["haiku", 7, None, "haiku"])
    s = section(claude(), state)
    assert ids(s.main)[0] == "haiku"
    assert ids(s.main).count("haiku") == 1


def test_prune_model_order_keeps_the_claude_code_default(state):
    state.set(ui_state.MODEL_ORDER_KEY, ["", "haiku", "claude-opus-3-0", "gpt-6.1"])
    removed = ui_state.prune_model_order(
        {ANTHROPIC: {"", "haiku"}}, state=state,
    )
    assert removed == 1
    assert state.get(ui_state.MODEL_ORDER_KEY) == ["", "haiku", "gpt-6.1"]


def test_alias_twins_ignore_the_date_suffix():
    twins = model_catalog.claude_alias_twins([
        ModelEntry("haiku", "Haiku 4.5", "Latest"),
        ModelEntry("claude-haiku-4-5-20251001", "Haiku 4.5 (2025-10-01)", "Haiku"),
        ModelEntry("claude-haiku-3-5", "Haiku 3.5", "Haiku"),
    ])
    assert twins == {"claude-haiku-4-5-20251001"}
