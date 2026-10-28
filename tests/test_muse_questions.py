"""Muse's answer contract, also exercised in GTK-free CI."""

import pytest

from helios.backend.process.muse_questions import question_payload, translate_answers


def prompt(*questions):
    return {"questions": list(questions)}


SINGLE = {"id": "which_url", "selection": {"mode": "single"},
          "options": [{"label": "example.com/ops"}, {"label": "Other site"}]}
MULTIPLE = {"id": "features", "selection": {"mode": "multiple"},
            "options": [{"label": "A"}, {"label": "B"}]}


@pytest.mark.parametrize("answer,expected", [
    ("example.com/ops", {"selectedLabel": "example.com/ops"}),
    ("example.com", {"freeText": "example.com"}),
    ({"which_url": {"answers": ["example.com"]}}, {"freeText": "example.com"}),
    ({"0": {"answers": ["example.com/ops"]}}, {"selectedLabel": "example.com/ops"}),
])
def test_squire_ops_choice_and_other_answer(answer, expected):
    assert translate_answers(prompt(SINGLE), answer) == [{"questionId": "which_url", **expected}]


@pytest.mark.parametrize("labels,expected", [
    (["A", "B"], {"selectedLabels": ["A", "B"]}),
    (["A", "Custom feature"], {"selectedLabels": ["A"], "note": "Custom feature"}),
    (["Custom feature"], {"freeText": "Custom feature"}),
])
def test_multi_select_preserves_choices_and_other_text(labels, expected):
    answer = {"features": {"answers": labels}}
    assert translate_answers(prompt(MULTIPLE), answer) == [{"questionId": "features", **expected}]


def test_native_ids_bind_answers_even_with_a_numeric_id_and_reordered_map():
    questions = prompt({"id": "1"}, {"id": "details"})
    answer = {"details": {"answers": ["second"]}, "1": {"answers": ["first"]}}
    assert translate_answers(questions, answer) == [
        {"questionId": "1", "freeText": "first"},
        {"questionId": "details", "freeText": "second"},
    ]
    assert [q["id"] for q in question_payload(questions)["questions"]] == ["1", "details"]


@pytest.mark.parametrize("length", [500, 501])
def test_native_free_text_length_limit_without_truncation(length):
    text = "x" * length
    if length == 500:
        assert translate_answers(prompt(SINGLE), text) == [{"questionId": "which_url", "freeText": text}]
    else:
        with pytest.raises(ValueError, match="500"):
            translate_answers(prompt(SINGLE), text)


def test_native_note_length_limit():
    with pytest.raises(ValueError, match="500"):
        translate_answers(prompt(MULTIPLE), {"features": {"answers": ["A", "x" * 501]}})


def test_dismissal_is_cancel_but_missing_answers_are_not():
    assert translate_answers(prompt(SINGLE), None) is None
    for answer in ({}, {"which_url": {"answers": []}}, ""):
        with pytest.raises(ValueError):
            translate_answers(prompt(SINGLE), answer)


def test_missing_or_duplicate_native_ids_are_not_guessed():
    for questions in (prompt({}), prompt({"id": "q"}, {"id": "q"})):
        with pytest.raises(ValueError, match="unique ID"):
            translate_answers(questions, {"0": "A", "1": "B"})
