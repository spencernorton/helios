# Adding a provider

Helios drives Claude (`claude` CLI), GPT (Codex App Server), Gemini
(Antigravity `agy`), Muse (`muse serve`), and OpenRouter (in-process
gateway). Kimi and other OpenAI-compatible models already arrive through
OpenRouter's live catalog — a native provider is only worth adding when a
vendor has its own CLI, subscription, or quota API the gateway cannot see.

Register once, then fill the seams. Most lists derive from the registry;
the driver spawn points do not, and this list names them honestly.

## Checklist

1. **Identity** — add a `ProviderDescriptor` in
   `backend/provider_identity.py` and include it in
   `DEFAULT_PROVIDER_REGISTRY`. Pick `credential_owner` carefully:
   `NATIVE_RUNTIME` puts the row on the Accounts page with terminal
   sign-in; `HELIOS_SUPERVISOR` marks a Helios-held key (no terminal).
2. **Catalog** — in `backend/model_catalog.py`: `PROVIDER_*` constant,
   `PROVIDER_LABELS` entry, an `*_entries()` discovery function (live
   probe, cached, never raises), `provider_for()` classification, and a
   `context_window_for()` branch. Wire the entries into
   `MainWindow._refresh_model_catalog` on its worker thread.
3. **Driver** — a driver in `backend/process/` emitting the common signal
   contract (`tests/test_driver_signal_contract.py` enforces it).
   Provider-specific signals connect in an `isinstance` branch in the
   window, never in `COMMON_DRIVER_SIGNALS`. Emit `usage-updated`
   (context) and `rate-limit-updated` (quota) whenever the vendor
   reports them; unknown limits stay unknown, never zero.
4. **Accounts** — a reader in `backend/accounts.py` plus a
   `terminal_command` branch when the CLI owns sign-in. The provider
   appears on the Accounts page automatically via the registry.
5. **Surfaces** — an empty-state line in `chat_toolbar._LIMITS_EMPTY`, a
   Providers page section in `settings_dialog.py`, and transcript
   resolution in `backend/session_providers.py` so old chats keep their
   provider chip.
6. **Tests** — catalog discovery (GTK-free), account read, and the
   driver signal contract. Run `python3 -m pytest -q` and `ruff check .`.

## Rules that keep it seamless

- Discovery must fail closed: no live proof, no selectable models.
- Credentials belong to the vendor CLI; Helios never copies them.
- Catalog refresh stays off the GTK main thread and off send paths.
