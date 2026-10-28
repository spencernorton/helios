"""GTK-free translation of Muse MSP questions and answers.

Contract: Muse 1.4.6's embedded MSP schema (`muse schema
generate-json-schema`), UserInputAnswerParams. Exactly one of selectedLabel,
selectedLabels or freeText is required; an optional note can accompany choices.
"""

from __future__ import annotations

from typing import Any


def question_payload(prompt: dict, *, caption: str = "") -> dict:
    questions = []
    for question in prompt.get("questions") or []:
        if not isinstance(question, dict):
            continue
        selection = question.get("selection") or {}
        questions.append({
            "id": str(question.get("id") or ""),
            "question": str(question.get("question") or ""),
            "header": str(question.get("header") or ""),
            "options": [{"label": str(o.get("label") or ""),
                         "description": str(o.get("description") or "")}
                        for o in (question.get("options") or []) if isinstance(o, dict)],
            "multiSelect": isinstance(selection, dict) and selection.get("mode") == "multiple",
        })
    return {"questions": questions, "caption": caption}


def translate_answers(prompt: dict, answer: Any) -> list[dict] | None:
    """Preserve native question IDs and distinguish choices from typed text.

    None means an explicit dismissal. Invalid/incomplete answers raise instead
    of silently cancelling the prompt. Index-keyed maps from older Helios
    callers remain accepted, but the dialog now uses native IDs throughout.
    """
    if answer is None:
        return None
    questions = [q for q in (prompt.get("questions") or []) if isinstance(q, dict)]
    ids = [str(q.get("id") or "") for q in questions]
    if not ids or not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("Muse supplied a question without a unique ID.")
    if isinstance(answer, str) and answer.strip() and len(questions) == 1:
        picked = [[answer.strip()]]
    elif isinstance(answer, dict):
        keys = ids if all(qid in answer for qid in ids) else [str(i) for i in range(len(ids))]
        picked = []
        for key in keys:
            entry = answer.get(key)
            raw = entry.get("answers") if isinstance(entry, dict) else entry
            if isinstance(raw, str):
                raw = [raw]
            picked.append([v.strip() for v in raw if isinstance(v, str) and v.strip()]
                          if isinstance(raw, list) else [])
    else:
        raise ValueError("Please answer each Muse question.")

    rows = []
    for question, question_id, labels in zip(questions, ids, picked):
        if not labels:
            raise ValueError("Please answer each Muse question.")
        options = {str(o.get("label") or "").strip(): str(o.get("label") or "")
                   for o in (question.get("options") or []) if isinstance(o, dict)}
        selected = [options[label] for label in labels if label in options]
        typed = [label for label in labels if label not in options]
        row = {"questionId": question_id}
        if selected:
            selection = question.get("selection") or {}
            if isinstance(selection, dict) and selection.get("mode") == "multiple":
                row["selectedLabels"] = selected
            elif len(selected) == 1:
                row["selectedLabel"] = selected[0]
            else:
                raise ValueError("Choose one answer for this Muse question.")
            if typed:
                row["note"] = "\n".join(typed)
        else:
            row["freeText"] = "\n".join(typed)
        for field in ("freeText", "note"):
            if len(row.get(field, "")) > 500:
                raise ValueError("Muse accepts at most 500 characters per typed answer; please shorten it.")
        rows.append(row)
    return rows
