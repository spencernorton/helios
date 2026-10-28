"""Public model metadata must agree with displayed choices and wire settings."""
from __future__ import annotations

import pytest

from helios.backend.model_catalog import ModelEntry
from helios.backend.openrouter.catalog import build_entries
from helios.backend.openrouter.chat import reasoning_for_effort
from helios.backend.project_perms import provider_allows_mode, permission_description


def _row(metadata):
    return {'id': 'vendor/model', 'context_length': 32000,
            'supported_parameters': ['reasoning', 'tools'], 'reasoning': metadata}


@pytest.mark.parametrize('mandatory', [False, True])
def test_openrouter_uses_model_efforts_and_never_disables_mandatory_reasoning(mandatory):
    entry = build_entries([_row({'supported_efforts': ['none', 'minimal', 'high', 'xhigh', 'max'],
        'default_effort': 'xhigh', 'mandatory': mandatory})])[0]
    keys = {key for key, _desc in entry.reasoning_efforts}
    assert keys == ({'minimal', 'high', 'xhigh', 'max'} | (set() if mandatory else {'off'}))
    assert entry.default_effort == 'xhigh'
    for key in keys - {'off'}:
        assert reasoning_for_effort(key) == {'effort': key}


def test_openrouter_omitted_efforts_are_not_universal_but_null_is():
    assert build_entries([_row({'mandatory': True})])[0].reasoning_efforts == ()
    entry = build_entries([_row({'supported_efforts': None, 'mandatory': True})])[0]
    assert {key for key, _ in entry.reasoning_efforts} == {'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def test_google_picker_exposes_only_distinct_headless_modes():
    from helios.backend.project_perms import permission_label
    assert not provider_allows_mode('google', 'auto')
    assert not provider_allows_mode('google', 'dontAsk')
    assert permission_label('default', provider='google') == 'Sandboxed'
    assert 'cannot show' in permission_description('default', provider='google')
    assert provider_allows_mode('google', 'acceptEdits')


def test_model_bound_native_effort_is_validated_on_change_and_restore(tmp_path):
    pytest.importorskip('gi')
    from helios.backend.process.muse_driver import MuseServeDriver
    entry = ModelEntry('muse-test', 'Test', 'Muse', reasoning_efforts=(('low', ''),), default_effort='low')
    driver = MuseServeDriver(cwd=str(tmp_path), model=entry.id, effort='max')
    driver.set_effort_catalog([entry])
    assert driver.effort_key == 'low'
    results = []
    assert not driver.set_effort('high', lambda ok, _detail: results.append(ok))
    assert results == [False]
    assert not driver.set_effort('invented')
    assert driver.effort_key == 'low'
    driver.set_effort_catalog([ModelEntry(entry.id, 'Test', 'Muse')])
    assert driver.effort_key == ''


def test_openrouter_mandatory_policy_applies_to_stored_and_live_effort(tmp_path):
    pytest.importorskip('gi')
    from helios.backend.process.openrouter_driver import OpenRouterDriver
    entry = build_entries([_row({'supported_efforts': ['high', 'max'], 'mandatory': True, 'default_effort': 'max'})])[0]
    driver = OpenRouterDriver(cwd=str(tmp_path), model=entry.id, effort='off')
    driver.set_effort_catalog([entry])
    assert driver.effort_key == 'max'
    assert not driver.set_effort('off')
    assert driver.set_effort('high')


def test_claude_explicit_no_effort_disables_control_and_rejects_change(tmp_path):
    pytest.importorskip('gi')
    from helios.backend.process.cli_driver import ClaudeCliDriver
    from helios.widgets.chat_toolbar import ChatToolbar
    driver = ClaudeCliDriver(cwd=str(tmp_path), model='no-effort')
    assert driver.supported_effort_levels() is None
    driver._cli_models = [{'value': 'no-effort', 'supportsEffort': False}]
    assert driver.supported_effort_levels() == []
    assert not driver.set_effort('high')
    toolbar = ChatToolbar()
    toolbar.set_anthropic_effort_options('high', supported_levels=[])
    assert not toolbar._effort_provider_ok


def test_muse_model_ack_drops_incompatible_previous_effort(tmp_path):
    pytest.importorskip('gi')
    import io
    import json
    from types import SimpleNamespace
    from helios.backend.process.muse_driver import MuseServeDriver
    driver = MuseServeDriver(cwd=str(tmp_path), model='muse-first', effort='high')
    driver.set_effort_catalog([
        ModelEntry('muse-first', 'First', 'Muse', reasoning_efforts=(('high', ''),)),
        ModelEntry('muse-second', 'Second', 'Muse', reasoning_efforts=(('low', ''),), default_effort='low'),
    ])
    driver._session_id = 'session'
    driver._accepting_input = True
    driver._proc = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
    assert driver.set_model('muse-second')
    assert driver.effort_key == 'high'
    frame = json.loads(driver._proc.stdin.getvalue().splitlines()[-1])
    driver._dispatch_message({'id': frame['id'], 'result': {'status': 'accepted'}})
    assert driver.effort_key == 'low'


def test_google_permission_labels_refresh_when_switching_providers():
    pytest.importorskip('gi')
    from helios.widgets.chat_toolbar import ChatToolbar
    toolbar = ChatToolbar()
    toolbar.set_model('gemini-test')
    toolbar.set_permission_mode('default')
    assert toolbar._permission_label.get_label() == 'Sandboxed'
    assert toolbar._permission_rows['default'][0]._permission_title.get_label() == 'Sandboxed'
    assert not toolbar._permission_rows['auto'][0].get_visible()
    toolbar.set_model('gpt-test')
    assert toolbar._permission_label.get_label() == 'Ask'
    assert toolbar._permission_rows['auto'][0].get_visible()


def test_claude_native_future_effort_is_not_filtered_by_static_constants():
    pytest.importorskip('gi')
    from helios.widgets.chat_toolbar import ChatToolbar
    toolbar = ChatToolbar()
    assert toolbar.set_anthropic_effort_options('future', supported_levels=['low', 'future']) == 'future'
    assert {key for key, _label, _desc in toolbar._effort_stops} == {'off', 'low', 'future'}


def test_live_effort_changes_require_model_discovery(tmp_path):
    pytest.importorskip('gi')
    from helios.backend.process.muse_driver import MuseServeDriver
    from helios.backend.process.codex_driver import CodexCliDriver
    for driver in [MuseServeDriver(cwd=str(tmp_path), model='muse-test'),
                   CodexCliDriver(cwd=str(tmp_path), model='gpt-test')]:
        result = []
        assert not driver.set_effort('high', lambda ok, _detail: result.append(ok))
        assert result == [False]


def test_implicit_effort_repair_never_mints_an_ultra_lease(tmp_path):
    pytest.importorskip('gi')
    from helios.backend.process.codex_driver import CodexCliDriver
    driver = CodexCliDriver(cwd=str(tmp_path), model='gpt-test', effort='unsupported')
    driver.set_effort_catalog([ModelEntry('gpt-test', 'GPT', 'OpenAI',
        reasoning_efforts=(('high', ''), ('ultra', '')), default_effort='ultra')])
    assert driver.effort_key == 'high'


def test_openrouter_disabled_default_does_not_enable_reasoning_implicitly():
    entry = build_entries([_row({'supported_efforts': ['high', 'low'], 'mandatory': False,
                                'default_effort': 'low', 'default_enabled': False})])[0]
    assert entry.default_effort == 'off'
    assert 'off' in {key for key, _desc in entry.reasoning_efforts}


def test_legacy_openrouter_cache_migrates_only_in_background(tmp_path, monkeypatch):
    import json
    import time
    from helios.backend import model_catalog as mc
    from helios.backend.openrouter import catalog as oc, key
    path = tmp_path / 'models.json'
    monkeypatch.setattr(oc, 'CACHE_PATH', path)
    monkeypatch.setattr(key, 'load_key', lambda: 'test-key')
    legacy = _row({'supported_efforts': ['high'], 'mandatory': True})
    legacy.pop('reasoning')
    path.write_text(json.dumps({'fetched_at': int(time.time()), 'rows': [legacy]}))
    fetched = []

    def refresh():
        fetched.append(True)
        rows = [_row({'supported_efforts': ['high'], 'mandatory': True})]
        oc._write_cache(rows)
        return oc.build_entries(rows)

    monkeypatch.setattr(oc, 'refresh_models', refresh)
    assert mc.openrouter_entries()[0][0].reasoning_efforts == ()
    assert fetched == []
    entries, status = mc.openrouter_entries(refresh_if_stale=True)
    assert status == 'fetched'
    assert entries[0].reasoning_efforts == (('high', 'High reasoning effort'),)
    assert len(fetched) == 1
    mc.openrouter_entries(refresh_if_stale=True)
    assert len(fetched) == 1


@pytest.mark.parametrize('efforts', [[], ['unknown'], 'high', 42])
def test_empty_or_malformed_reasoning_metadata_never_forces_off(efforts):
    entry = build_entries([_row({'supported_efforts': efforts, 'mandatory': False,
                                'default_enabled': True})])[0]
    assert entry.reasoning_efforts == ()
    assert entry.default_effort == ''
