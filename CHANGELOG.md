# Changelog

All notable changes to Helios are documented here. Each release is a
`vX.Y.Z` tag on `main`; its GitHub Release carries the `.deb`, the tagged
source (`source.tar.gz`) and `SHA256SUMS.txt`.

## 0.114.1 — 2026-10-28

- Muse background continuations now stream and persist their replies, and active workflows, subagents and tools protect their sessions from idle eviction.
- Muse typed question answers use the native free-text field; rejected answers remain available to retry.
- Active Goal instructions ask agents to complete all unblocked steps before yielding.

## 0.114.0 — 2026-10-26

- Native Kimi Code membership provider over ACP: managed sign-in, live model/thinking catalogs, streaming turns and plans, tool approvals, images/resources, MCP forwarding, exact resume/fork, native Compact, durable Work receipts and transcripts, Settings/Accounts integration, and provider-safe session routing. Audio and unsupported ACP methods are not advertised.

## 0.113.0 — 2026-10-22

- *Chat in a Worktree* (new-chat menu): an opt-in git worktree of the current repository on its own `helios/…` branch under `~/.helios/worktrees/`, with a new chat in it. The Changes pane commits, pushes and opens a PR from there, and *Remove worktree* deletes the folder once it is clean, keeping the branch. The automatic redirect removed in 0.66.0 stays removed.

## 0.112.0 — 2026-10-22

- Browser pane: new agent tools `browser_click` (selector or visible text), `browser_type` (replace a field's value the way frameworks notice, optionally submit) and `browser_console` (recent console messages and uncaught errors, captured from document start), under the existing per-site consent. A camera button attaches a screenshot of the current page to your message.
- Terminal pane *Run* menu: starts a configuration from `.claude/launch.json` (Claude desktop's Preview format, JSONC tolerated) or a `package.json` `dev`/`start` script in its own tab, then opens `localhost:PORT` in the browser pane once the port answers.

## 0.111.0 — 2026-10-22

- New **Terminal** workspace pane: tabs of your own shell starting in the chat's folder (VTE; `gir1.2-vte-3.91` is recommended). *Send to chat* puts the selected output into the message box as a code block.
- New **Background** workspace pane: the shells, monitors and agents a Claude chat is running between turns (from `background_tasks_changed`), each with *Stop* (the CLI's `stop_task`) and *Output*, which opens the task's live output file in the side pane.

## 0.110.0 — 2026-10-22

- Each saved message of yours has a ⋮ menu: *Rewind code to before this message* opens Rewind on the checkpoint taken just before it, and *Fork conversation from here* (Claude chats) copies the conversation up to that message into a new chat, opens it and puts the message back in the composer to edit and resend. The source conversation is never modified; the Claude CLI resumes the fork with only the earlier context (verified live).

## 0.109.0 — 2026-10-22

- The Changes pane is now a review pane. Diffs are split into hunks under *Staged* and *Unstaged*, with per-hunk and per-file Stage, Unstage and Discard (discards confirm; untracked files go to the trash). A comment button on each hunk collects notes, and *Send N comments to agent* delivers them to the current chat as one message. A commit bar commits staged changes, pushes the branch (setting upstream the first time) and opens a PR/MR with `gh` or `glab` in a terminal.

## 0.108.0 — 2026-10-22

- Send images to the model. Paste an image from the clipboard, drop an image file, or take a screenshot with the new camera button (desktop screenshot portal; works on Wayland). The image is attached as an editable `@path` reference, shown as a thumbnail above the composer, and sent natively: Claude gets image content blocks, GPT gets Codex `localImage` input, Muse gets MSP `image` input. All three were verified end to end (each named the colours of a test image with its tools off). Gemini and OpenRouter still receive the path as text.
- The message you send shows its images immediately, and saved GPT, Gemini and Muse conversations keep them.
- `gir1.2-xdp-1.0` and `gir1.2-xdpgtk4-1.0` are recommended for screenshots.

## 0.107.0 — 2026-10-22

- Images are visible. The transcript parser kept only text, so every image a tool returned, a screenshot from the browser pane, or a picture you attached was dropped. Images now render as thumbnails under the message (Claude tool results and attachments; Codex `imageView`, `imageGeneration` and MCP image results; Muse tool images). Click opens the full image in the side pane, which also renders PDFs and HTML; right-click copies it.
- Tool cards that touched a file (Read, Write, Edit) have an open button that shows the file in the side pane.
- Inline images are written once to `~/.helios/media/` and provider mirrors (GPT, Gemini, Muse, OpenRouter) store a path, so a reopened chat shows the same images without base64 in the transcript.

## 0.106.0 — 2026-10-22

- Add a tabbed browser pane beside the transcript (Ctrl+Shift+B or the header
  button) built on WebKitGTK 6: back, forward, reload, new tab, open in your
  browser, and an address bar that takes a URL, `localhost:PORT` or a file path
  relative to the chat's folder. It keeps its own persistent profile under
  `~/.helios/browser/`, separate from your browser.
- Let agents drive it through the new `helios-browser-mcp` server
  (`browser_open`, `browser_navigate`, `browser_read`, `browser_screenshot`,
  `browser_tabs`), injected per session for Claude, GPT (Codex thread config),
  Muse (`sessionMcp`) and OpenRouter. Files in the chat's folder are served
  through a contained `helios-file://` scheme that refuses anything outside that
  folder (symlinks and frames included) and serves a folder only to its own tab,
  and loopback servers need no approval; an agent opening, reloading or reading any other
  site, file or popup asks you first (the answer covers that tab while it stays
  there; "Always allow" is remembered per host), and tabs you navigated yourself
  are listed as "(not shared)". Each session's folder is bound to a token
  Helios issues at launch, so nothing else on the session bus can claim one.
  The pane's profile blocks third-party cookies, so a page framing a site you
  are signed in to does not carry the sign-in. Large screenshots are halved to
  stay within every provider's message limit. Gemini
  is not wired: Antigravity has no per-session MCP option.
- The package now depends on `gir1.2-webkit-6.0`; a source checkout without it
  shows an install hint in the pane instead of failing.

## 0.105.4 — 2026-10-22

- Show context fill and the tightest account limit beside the context ring
  (`84.2k / 200k · 5-hour 55%`), so usage is readable without hovering. A
  provider that publishes no window size reads `13.1k used` rather than a guess.
- Feed the context meter from every provider: Muse now uses MSP
  `session/contextUsage` instead of cumulative prompt/output counts (which drew
  a near-full red ring), Google derives the fill from each finished
  Antigravity model call, and OpenRouter updates the ring after every tool round
  and no longer zeroes it when a turn fails before any reported request.
- Report Muse's 5-hour and weekly subscription windows (`usage/changed`, plus a
  `usage/read` at session start) in Account usage limits, and tag OpenRouter
  429 rows with the `openrouter` provider so they are no longer filtered out.
- Keep each session's last context fill and each provider's limit rows across
  session and provider switches, and accept account limits reported by a
  background session.
- Add per-session token totals ("This session: 39.0k in · 4 out") to the
  context popover, cap the popover's wrapped notes so it no longer outgrows the
  window and clips the per-category token counts, label Muse replies "Muse"
  instead of "Claude", and give Muse and Google accurate empty-state text.
- Count what a whole Codex turn consumed (growth of App Server's thread total)
  in its footer and session totals instead of only its final request, count
  Google's cached input toward "in", clear a shown OpenRouter rate limit after
  the next successful request, and show a Google fill in the popover and ring
  tooltip instead of guessing a 1M window.

## 0.105.3 — 2026-10-22

- Claude sessions waiting on background work are no longer shut down. A turn can end while background shells, monitors or subagents keep running (the CLI wakes the session when they finish); the 10-minute idle reaper and the live-session cap treated that session as idle and ended it, killing the work. The driver now tracks `system/background_tasks_changed` and both leave such sessions alone.
- The sidebar dot for a session Helios is running follows the driver: mid-turn or waiting on background work shows as working. Read from the transcript alone, the dot flipped to ready mid-turn whenever the newest record was text, under a "working" caption.

## 0.105.2 — 2026-10-22

- Muse's context meter is real: the driver sent cumulative (prompt, output) token totals as the meter's (used, window), so it read "2k max, 100% used". It now reports Muse's own `session/contextUsage` (`usedTokens` / `windowTokens`, ~1M). Muse manages and compacts its context itself; this was a display bug.
- A provider that reports no context window (Gemini via agy) clears the meter and says so, instead of keeping the previous model's numbers.
- Switching models on a chat with nothing started is silent; the picker already shows the choice. Toasts now always close after their timeout, even with the pointer resting on them (Adw pauses the countdown on hover, and toasts appear where the picker leaves the pointer).

## 0.105.1 — 2026-10-22

- Accounts → Gemini shows the signed-in Google account after an agy `/login`. A fresh agy sign-in keeps its token in the system keyring instead of a file, so the row read as "Not signed in"; Helios now falls back to the account agy records in its own log.

## 0.105.0 — 2026-10-22

- New **Accounts** page, first in Settings: every provider on one list with the signed-in email and plan (Claude via `claude auth status`, GPT via Codex App Server `account/read`, Gemini from agy's sign-in token, Muse via `account/read`), and *Sign in…*, *Sign out* and *Switch account…* buttons that open each CLI's own flow in a terminal. The page re-reads accounts when you return; a changed account refreshes the model pickers. OpenRouter's row opens its key on *Providers*. The Claude-only account block on *Settings* and Muse's separate sign-in row are gone; Claude now has its own group on *Providers*.
- Fix Codex App Server probes hanging until timeout about one call in three: the reader waited on the pipe with `select()` but read through a buffered `readline()`, so a response that arrived in the same chunk as a notification was invisible. Model-catalog discovery shared the bug.

## 0.104.2 — 2026-10-22

- A picked model is no longer silently replaced. Starting a new chat left the previous row selected in the sidebar, and a later re-render (deleting a session, changing the filter) re-selected a row, which re-adopted that row's provider: picking Muse Full and sending spawned Claude. A new chat now clears the sidebar selection, and delete and filter changes keep the current selection instead of auto-selecting the newest row (delete still moves on when the open chat itself was deleted).
- Model changes switch in place instead of restarting: GPT sends `model` on every turn, Muse uses the native `session/setModel`, Claude keeps its control-channel switch. Reasoning changes on Muse are per-turn. A model picked while a turn runs is applied in place before the next turn. Only Gemini (model fixed at spawn) and OpenRouter resume the same conversation on the new model; that costs latency, not extra model tokens, since a new model reads the conversation uncached either way.
- Show Muse reasoning: the driver no longer wipes streamed `summary.n` parts when Muse re-sends an empty summary, falls back to committed raw reasoning text, and separates parts. Gemini (agy headless) carries no thinking text, so it still shows only the activity state.
- New sessions appear in the sidebar once their transcript lands (Gemini/Muse/Codex write it only after the first turn is accepted): a one-second file check for up to 30 s, then a single list rebuild.
- Deleting a session no longer leaves a "Session <id>" ghost: Helios stops its own driver before removing the file, refuses (with an explanation) sessions held by a `claude` process it did not start, and treats an already-missing file as deleted.
- A participant that is still starting or mid-turn counts as executing, so switching providers no longer fails with "This Work is already executing". Switching to a provider with no conversation in the Work says it cannot see the other chat; an unverifiable participant explains the refused send; the "preparing this chat" notice names Muse correctly.
## 0.104.1 — 2026-10-14

- Re-adopt a staged session's provider when the background model catalog finishes loading at startup, and block cross-provider sends while the initial catalog is still probing so resuming a Muse or Google session cannot switch to a stale persisted model or start a fresh contextless session.
- Keep initializing Muse and Google resume drivers (`is_initializing`) bound across sidebar refreshes, session routing, and `DriverManager` lookups before `session-started` arrives, preventing duplicate resume processes.

## 0.104.0 — 2026-10-13

- Ship `helios-router-mcp`, `helios-estate-mcp`, and `setup-codex-estate` in
  `/usr/lib/helios/` for packaged `.deb` installs and resolve the packaged
  launcher path when no source checkout is present.
- Upgrade `setup-codex-estate` to support `--replace-manifest`, `--sync-gemini`,
  and `--sync-muse` so all non-Claude CLI providers can share credential-free
  stdio MCP proxy entries, and surface Muse MCP servers in Settings → Tools.
- Grant the work-tracker token to interactive Google sessions at parity with
  the other interactive providers, and populate Google and Muse tool/MCP
  inventories for the Capabilities pane.
- Default session title generation to `first_message` (no model call), align
  Settings built-in model keys with the local engine, hide the Ollama status
  row unless Ollama is selected, and require an existing `llama-cli` binary
  rather than downloading unverified archives at runtime.
- Route `app.quit` (`Ctrl+Q`) through each window's close-request shutdown path
  before exiting so active drivers and UI timers terminate cleanly.
- Account for tool-schema token overhead during OpenRouter pre-round compaction,
  bound oversized single tool outputs via `ContextArchive`, and honor
  `Retry-After` headers on HTTP 429 responses.
- Bind `HELIOS_STATE_DIR`-backed module path attributes dynamically so runtime
  environment changes redirect all state stores without reloading modules.

## 0.103.3 — 2026-10-13

- Add Muse parity test coverage: native provider identity and routing,
  permission-mode parity, fail-closed discovery without a Muse binary or
  a working `muse serve` probe, and the Muse sidebar provider chip.
  No runtime behavior change.

## 0.103.2 — 2026-10-13

- Give Muse a working shell in Bypass: Bypass now starts `muse serve
  --disable-sandbox`, matching Codex full access. On Ubuntu, Muse rejects
  stock bubblewrap and its embedded copy is denied a user namespace, so
  every sandboxed shell call failed. Ask keeps the sandbox; Plan stays
  read-only. Muse sessions also receive the tracker token like the other
  interactive providers.

## 0.103.1 — 2026-10-13

- Fix Muse sessions being rejected as an "unsafe provider identity" on
  send: the window's provider allowlist missed Muse, so every Muse
  session was torn down unsent. Muse also joins transcript-detach,
  session-handoff labels, and the work-store concurrency lanes at parity
  with the other native providers.

## 0.103.0 — 2026-10-12

- Make model selection navigable: the chat picker always shows its search
  field, the Settings default-model dropdown is searchable, and the current
  model carries a checkmark in the list. Duplicate rows are removed at every
  merge point, Anthropic latest aliases collapse to one row per family (the
  twins and pinned versions stay a disclosure or a search away), and
  Settings → Providers → Shown models can hide any model from all pickers
  and bring it back. The selected model is never hidden.

## 0.102.0 — 2026-10-12

- Add Muse (Meta) as a native provider: Helios drives Muse Code over
  `muse serve` (MSP) with the same streaming, tool cards, subagent/workflow
  dock animations, approvals, and durable accounting as the other native
  providers. Sign in with `muse login` or a Meta API key in
  Settings → Providers; models come from the live `model/list` catalog and
  are selectable only when the CLI proves them. The CLI keeps every
  credential — Helios stores no copy.

## 0.101.5 — 2026-10-12

- Fix Google sessions getting stuck after native acceptance: retain the native
  input-step identity in both acceptance and completion receipts, and normalize
  Google's token counters for durable accounting.
- Keep Google user and queued-message bubbles provisional until the CLI confirms
  receipt. Preserve the next draft during initialization instead of launching a
  competing session.
- Restore Google's activity-clock contract for idle reaping and LRU eviction,
  record the effective model and permissions, and avoid cancellation errors when
  intentionally stopping a completed chat.
- Run native Google driver, window-accounting and checkpoint regressions with
  GTK under Xvfb in the required CI check.

## 0.101.4 — 2026-10-10

### Fixed

- User prompts submitted while the Google CLI driver initializes are buffered as pending rather than rejected, automatically dispatching to the native CLI once native initialization completes.
- Aborted or interrupted Google session startups cleanly release buffered prompt attempts and execution lanes.

## 0.101.3 — 2026-10-10

### Fixed

- Google provider identity is recognized by the session manager and live driver registry so native session starts are accepted rather than rejected as unknown provider identities.
- Google assistant labels and transcript views display the proper provider name and avoid overwriting Claude's tool snapshot.

## 0.101.2 — 2026-10-10

### Fixed

- Google model selection groups reasoning variants (low, medium, high) into base models with the effort level selected through the execution toolbar instead of displaying separate model dropdown entries.
- Google sessions support all standard permission modes (Ask, Accept edits, Auto, Bypass, Plan, Never ask) with appropriate CLI flags (`--dangerously-skip-permissions`, `--mode plan`, `--mode accept-edits`, `--sandbox`).

## 0.101.1 — 2026-10-10

### Fixed

- Google models and labels now come from the live Antigravity catalog, without guessed models or context sizes. Account sign-in is verified through native quota results.
- Google sessions use Antigravity's persistent streaming protocol, native conversation IDs and terminal results, including guarded Work admission and uncertain-delivery handling.
- API/custom provider configurations and AI-credit fallback are refused on the Google account route; the separate Gemini CLI is no longer treated as an interchangeable transport.
- Google Settings shows the actual account/discovery status and refreshes model choices without blocking the main loop.

### Changed

- Google sessions offer Never ask with terminal sandboxing; workspace edits remain possible under the native CLI's policy. Added setup instructions and a separate-worktree trial workflow.

## 0.101.0 — 2026-10-06

### Added

- Native Google subscription support via Gemini CLI / Antigravity (`agy` / `gemini`).
- Unified session model dropdown grouped by provider with direct subscription routing.

### Changed

- Removed the segmented Claude / GPT / OpenRouter provider toggle from the main header bar in favor of per-session model selection.
- OpenRouter catalog strictly excludes models from native subscription providers (Anthropic, OpenAI, Google).

## 0.100.0 — 2026-10-06

### Changed

- Claude usage limits now show native percentages and readable weekly/extra-usage labels; active tool progress shows the CLI's elapsed time.
- Codex hook failures and blocking decisions appear as concise transcript notices, with private context excluded.
- The Codex protocol compatibility manifest now targets CLI 0.160.1.

### Fixed

- Codex quota buckets no longer inherit another bucket's windows or blocked status, and recovered limits and removed windows update correctly.
- Unavailable quota percentages stay unknown, and unchanged quota rows avoid redundant widget rebuilding.

## 0.99.8 — 2026-10-04

### Fixed

- The Release workflow's build artifact is now `helios-debian-candidate` and carries the package with its own `SHA256SUMS.txt`, so a downstream archive can check that the released `.deb` is byte-for-byte the one CI built for the tag.

## 0.99.7 — 2026-10-04

### Changed

- Extra instructions for every session (Claude, GPT and OpenRouter) now come from optional `*.md` files in `$XDG_CONFIG_HOME/helios/system-prompt.d/`, read in name order at startup. Helios no longer ships a built-in work-tracker policy.
- The cross-machine session pool defaults to `$XDG_DATA_HOME/helios/session-pool`; `HELIOS_SESSION_POOL` still overrides it, and the pool stays inert until the directory exists.
- The Shared pane's endpoint and key are read from `HELIOS_SCRATCHPAD_URL` and `HELIOS_SCRATCHPAD_KEY` (renamed; set these if you use the pane). Both are stripped from agent environments.
- `scripts/build-deb.sh` is now `scripts/build.sh`: it builds the `.deb` only and runs `lintian` when installed. Releases are built and published by the repository's Release workflow.
- Development happens in this repository: pull requests are reviewed and squash-merged here.
- CI runs on `ubuntu-24.04`, and the README footer links the whole suite.
- `system-prompt.d` is capped at 64 KiB in total; a file that would pass the cap is skipped with a warning.
- `scripts/install-helios-router-service` takes the client user from `HELIOS_ROUTER_CLIENT_USER` or `SUDO_USER` and no longer has a built-in default.

### Fixed

- The router service installer works from a source checkout: `deploy/helios-router.service.in` is now part of the tree.
