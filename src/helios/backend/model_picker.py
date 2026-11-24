"""Which models the chat picker shows up front, and how the user curates that.

Every provider's catalog splits into two tiers:

* **Your list** — shown up front. Until the user curates a provider it is that
  provider's *current* models: the floating alias of each Claude family (named
  after the version it resolves to today), the first rows Codex, Gemini, Muse
  and Kimi report, or the OpenRouter shortlist ticked in Settings (none until
  something is ticked: nobody picks from several hundred rows). Models the
  user adds join it and models the user hides leave it.
* **More** — everything else the provider offers: older pinned versions,
  hidden models, and the "Claude Code default" sentinel. Folded behind one
  disclosure per provider; the search box always covers both tiers.

Hiding never deletes a row, it moves the row to More, so every catalog model
stays one click away. A pinned Claude id that names the same model as an alias
row (``claude-fable-5-2`` beside the ``fable[1m]`` row labelled "Fable 5.2") is
a twin and is not listed at all unless it is the selection or the user placed
it in their list.

Curation lives in two ui-state keys:

``hidden_models``
    Ids removed from a provider's default list.
``model_order``
    Ids the user has placed in their list, in order. The first curation of a
    provider writes that provider's whole list here, so from then on its order
    is the user's. Default models that appear later (a new Claude family, a new
    Codex model) join the end of the list until hidden.

Everything here is GTK-free so the slim CI lane can exercise it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from helios.backend import model_catalog, ui_state
from helios.backend.model_catalog import ModelEntry

#: Display order of provider sections; unknown providers follow.
PROVIDER_ORDER: tuple[str, ...] = (
    model_catalog.PROVIDER_ANTHROPIC,
    model_catalog.PROVIDER_OPENAI,
    model_catalog.PROVIDER_GOOGLE,
    model_catalog.PROVIDER_MUSE,
    model_catalog.PROVIDER_KIMI,
    model_catalog.PROVIDER_OPENROUTER,
)

#: Section titles in the picker: the short names toasts and chips already use.
PROVIDER_TITLES: dict[str, str] = dict(model_catalog.PROVIDER_LABELS)

#: Providers whose catalog arrives already curated (Codex App Server returns a
#: handful of agent models) show this many rows up front by default.
DEFAULT_LIST_CAP = 8


@dataclass(frozen=True, slots=True)
class PickerSection:
    """One provider's slice of the picker."""

    provider: str
    main: tuple[ModelEntry, ...]
    more: tuple[ModelEntry, ...]
    #: The ids this provider's list shows before any curation.
    default_ids: frozenset[str]
    #: True once the user has hidden or placed any of this provider's models.
    customized: bool
    #: Every id this provider's catalog offered when the section was built.
    catalog_ids: frozenset[str] = frozenset()

    @property
    def title(self) -> str:
        return section_title(self.provider)

    @property
    def main_ids(self) -> list[str]:
        return [entry.id for entry in self.main]

    def holds(self, model_id: str) -> bool:
        return model_id in self.catalog_ids


def section_title(provider: str) -> str:
    return PROVIDER_TITLES.get(provider, provider.capitalize())


def _ids(raw: object) -> list[str]:
    """A stored id list, tolerating a missing or hand-edited value."""
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(item for item in raw if isinstance(item, str)))


def _default_ids(
    provider: str,
    rows: list[ModelEntry],
    *,
    shortlist: set[str],
) -> list[str]:
    if provider == model_catalog.PROVIDER_ANTHROPIC:
        aliases = [entry.id for entry in rows if entry.group == "Latest"]
        if aliases:
            return aliases
        # No alias rows (an unusual CLI build): the newest pin of each family.
        # Pins arrive newest-first per family, so the first one wins.
        seen: set[str] = set()
        newest: list[str] = []
        for entry in rows:
            if entry.id and entry.group not in seen:
                seen.add(entry.group)
                newest.append(entry.id)
        return newest
    if provider == model_catalog.PROVIDER_OPENROUTER:
        # Exactly the Settings shortlist. Nobody picks from several hundred
        # rows, so with no shortlist the catalog waits under More and search.
        return [entry.id for entry in rows if entry.id in shortlist]
    return [entry.id for entry in rows[:DEFAULT_LIST_CAP]]


def sections(
    entries: Iterable[ModelEntry],
    *,
    hidden: Iterable[str] = (),
    order: Iterable[str] = (),
    openrouter_shortlist: Iterable[str] = (),
    selected: str = "",
) -> list[PickerSection]:
    """Split the catalog into per-provider sections, in display order."""
    hidden_set = set(_ids(list(hidden)))
    order_ids = _ids(list(order))
    shortlist = set(_ids(list(openrouter_shortlist)))
    placed = set(order_ids)

    by_provider: dict[str, list[ModelEntry]] = {}
    for entry in model_catalog.dedupe_entries(list(entries)):
        by_provider.setdefault(entry.provider, []).append(entry)
    providers = [p for p in PROVIDER_ORDER if p in by_provider]
    providers += [p for p in by_provider if p not in PROVIDER_ORDER]

    out: list[PickerSection] = []
    for provider in providers:
        rows = by_provider[provider]
        by_id = {entry.id: entry for entry in rows}
        customized = bool((hidden_set | placed) & by_id.keys())
        defaults = _default_ids(provider, rows, shortlist=shortlist)
        main_ids = [i for i in order_ids if i in by_id and i not in hidden_set]
        main_ids += [
            i for i in defaults if i not in hidden_set and i not in main_ids
        ]
        twins = (
            model_catalog.claude_alias_twins(rows)
            if provider == model_catalog.PROVIDER_ANTHROPIC
            else set()
        )
        in_main = set(main_ids)
        more = [
            entry for entry in rows
            if entry.id not in in_main
            and (entry.id not in twins or entry.id == selected)
        ]
        # The settings-default sentinel is a different kind of choice from a
        # model version; it closes the list rather than splitting it.
        more.sort(key=lambda entry: entry.id == "")
        out.append(PickerSection(
            provider=provider,
            main=tuple(by_id[i] for i in main_ids),
            more=tuple(more),
            default_ids=frozenset(defaults),
            customized=customized,
            catalog_ids=frozenset(by_id),
        ))
    return out


def stored_sections(
    entries: Iterable[ModelEntry],
    *,
    selected: str = "",
    state: ui_state.UiStateStore | None = None,
) -> list[PickerSection]:
    """:func:`sections` with the curation read from ui-state."""
    store = state if state is not None else ui_state.store()
    return sections(
        entries,
        hidden=_ids(store.get(ui_state.HIDDEN_MODELS_KEY, [])),
        order=_ids(store.get(ui_state.MODEL_ORDER_KEY, [])),
        openrouter_shortlist=_ids(store.get(ui_state.OPENROUTER_PICKER_KEY, [])),
        selected=selected,
    )


def matches(entry: ModelEntry, needle: str) -> bool:
    """The search predicate: id, label, group or provider, case-insensitive."""
    needle = needle.strip().lower()
    if not needle:
        return True
    return (
        needle in entry.id.lower()
        or needle in entry.label.lower()
        or needle in (entry.group or "").lower()
        or needle in entry.provider.lower()
        or needle in section_title(entry.provider).lower()
    )


# ── Curation ───────────────────────────────────────────────────────────────


def _store(state: ui_state.UiStateStore | None) -> ui_state.UiStateStore:
    return state if state is not None else ui_state.store()


def _save(
    store: ui_state.UiStateStore,
    section: PickerSection,
    main_ids: list[str],
    hidden: set[str],
) -> None:
    """Persist one provider's list as the user's order, plus the hidden set.

    Placed ids this catalog does not offer right now stay put: a curation made
    while only the fallback list (or a partial refresh) is loaded must not
    forget models that return with the full catalog. Only an explicit reset or
    authoritative pruning removes them.
    """
    order = [
        i for i in _ids(store.get(ui_state.MODEL_ORDER_KEY, []))
        if model_catalog.provider_for(i) != section.provider
        or not section.holds(i)
    ]
    order += [i for i in dict.fromkeys(main_ids) if i not in order]
    store.update(**{
        ui_state.HIDDEN_MODELS_KEY: sorted(hidden),
        ui_state.MODEL_ORDER_KEY: order,
    })


def _set_shortlisted(store: ui_state.UiStateStore, model_id: str, on: bool) -> None:
    picked = set(_ids(store.get(ui_state.OPENROUTER_PICKER_KEY, [])))
    if on:
        picked.add(model_id)
    else:
        picked.discard(model_id)
    store.set(ui_state.OPENROUTER_PICKER_KEY, sorted(picked))


def hide_model(
    section: PickerSection,
    model_id: str,
    *,
    state: ui_state.UiStateStore | None = None,
) -> None:
    """Move a model from the user's list to More."""
    store = _store(state)
    hidden = set(_ids(store.get(ui_state.HIDDEN_MODELS_KEY, [])))
    # Only a default needs remembering; a model the user added simply leaves
    # the order. The "" sentinel is never a default, so it never lands here.
    if model_id and model_id in section.default_ids:
        hidden.add(model_id)
    _save(store, section, [i for i in section.main_ids if i != model_id], hidden)
    if section.provider == model_catalog.PROVIDER_OPENROUTER:
        _set_shortlisted(store, model_id, False)


def add_model(
    section: PickerSection,
    model_id: str,
    *,
    state: ui_state.UiStateStore | None = None,
) -> None:
    """Move a model from More to the end of the user's list."""
    store = _store(state)
    hidden = set(_ids(store.get(ui_state.HIDDEN_MODELS_KEY, [])))
    hidden.discard(model_id)
    main = section.main_ids
    if model_id not in main:
        main.append(model_id)
    _save(store, section, main, hidden)
    if section.provider == model_catalog.PROVIDER_OPENROUTER:
        _set_shortlisted(store, model_id, True)


def move_model(
    section: PickerSection,
    model_id: str,
    index: int,
    *,
    state: ui_state.UiStateStore | None = None,
) -> bool:
    """Place a listed model at ``index`` within its provider's list."""
    main = section.main_ids
    if model_id not in main:
        return False
    current = main.index(model_id)
    main.remove(model_id)
    index = max(0, min(index, len(main)))
    if index == current:
        return False
    main.insert(index, model_id)
    store = _store(state)
    _save(store, section, main, set(_ids(store.get(ui_state.HIDDEN_MODELS_KEY, []))))
    return True


def reset_provider(
    provider: str,
    *,
    state: ui_state.UiStateStore | None = None,
) -> None:
    """Forget one provider's hides and order. OpenRouter keeps its Settings
    shortlist, which is that provider's default list."""
    store = _store(state)
    keep = [
        i for i in _ids(store.get(ui_state.HIDDEN_MODELS_KEY, []))
        if model_catalog.provider_for(i) != provider
    ]
    order = [
        i for i in _ids(store.get(ui_state.MODEL_ORDER_KEY, []))
        if model_catalog.provider_for(i) != provider
    ]
    store.update(**{
        ui_state.HIDDEN_MODELS_KEY: sorted(keep),
        ui_state.MODEL_ORDER_KEY: order,
    })


def reset_all(*, state: ui_state.UiStateStore | None = None) -> None:
    """Forget every provider's hides and order."""
    _store(state).update(**{
        ui_state.HIDDEN_MODELS_KEY: [],
        ui_state.MODEL_ORDER_KEY: [],
    })


def customization_counts(
    *, state: ui_state.UiStateStore | None = None,
) -> tuple[int, int]:
    """(hidden, placed) id counts, for the Settings summary."""
    store = _store(state)
    return (
        len(_ids(store.get(ui_state.HIDDEN_MODELS_KEY, []))),
        len(_ids(store.get(ui_state.MODEL_ORDER_KEY, []))),
    )


def sync_openrouter_shortlist(
    previous: Iterable[str],
    current: Iterable[str],
    *,
    state: ui_state.UiStateStore | None = None,
) -> None:
    """Keep the picker in step with a Settings edit of the OpenRouter shortlist.

    A model ticked in Settings must show even if it was hidden from the picker
    earlier; one unticked must leave the list even if it was placed in the
    user's order. Clearing the shortlist clears OpenRouter's curation too.
    """
    before, after = set(previous), set(current)
    store = _store(state)
    hidden = set(_ids(store.get(ui_state.HIDDEN_MODELS_KEY, [])))
    order = _ids(store.get(ui_state.MODEL_ORDER_KEY, []))
    hidden -= after - before
    dropped = before - after
    order = [i for i in order if i not in dropped]
    if not after:
        is_or = lambda i: model_catalog.provider_for(i) == model_catalog.PROVIDER_OPENROUTER  # noqa: E731
        hidden = {i for i in hidden if not is_or(i)}
        order = [i for i in order if not is_or(i)]
    store.update(**{
        ui_state.HIDDEN_MODELS_KEY: sorted(hidden),
        ui_state.MODEL_ORDER_KEY: order,
    })
