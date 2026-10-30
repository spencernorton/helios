# Helios user guide

Helios is a Linux desktop for coding agents you already use: Claude Code,
OpenAI Codex, Google Antigravity, Muse Code, Kimi Code and OpenRouter. Native
providers keep their own sign-in and session context; Helios supplies its own
runtime for OpenRouter. See the [quickstart](quickstart.md) for a first chat,
[Google setup](google-subscription.md) for Antigravity, and
[Kimi setup](kimi-subscription.md) for membership authentication.

Helios reads Claude transcripts and writes Claude-compatible display mirrors
for its other providers under `~/.claude/projects/`. These mirrors do not
replace a native provider's authoritative context. Housekeeping moves idle
sessions to `~/.helios/session-archive/` and dead temporary projects to
`~/.claude/projects-archive/`. Deleting a session from the sidebar asks for
confirmation; the maintenance cleanup rules are described in
[SECURITY.md](../SECURITY.md#what-helios-does-with-credentials-and-data).

**Install and launch.** Follow [Install](../README.md#install) for a current
release package or a source checkout. The default stable APT channel can lag
behind the latest release and may not include every feature in this guide.
The `helios` command (or `./scripts/helios` from source) takes no options of
its own; a second launch activates the running window.

## First run

**The window.** Left, the sessions sidebar; centre, the transcript and the
composer; right, an optional workspace pane. The header holds
*New chat* (`Ctrl+N`, or `Ctrl+Shift+N`
to pick the folder first), search (`Ctrl+F`), reload, Settings, and the
workspace-pane switcher.

**Before the first chat.** Set Settings → Defaults → *Default working
directory* (or `default_cwd` in `~/.helios/ui-state.json`). With nothing set,
a new chat opens in the most recent project folder Helios knows about, or in
`$HOME` on a fresh install — and a chat whose folder *is* `$HOME` is forced
read-only whatever mode you pick, with a toast that says so. Subfolders of
`$HOME` are not affected. `Ctrl+Shift+N` picks a folder for one chat.

**Where sessions come from.** Claude sessions are the transcripts Claude Code
writes under `~/.claude/projects/<encoded-cwd>/`, so the last four days of
terminal sessions are already in the sidebar, as one list ordered by what
needs attention (working, stuck, errored, then finished) and newest first.
GPT sessions are Codex App Server threads; OpenRouter sessions live under
`~/.helios/openrouter-sessions/`. The sidebar filter narrows the list to *All
sessions*, *This machine*, one folder, one other machine, or *Temporary*.

Sessions whose folder is under `/tmp` or has a path component starting with
`_tmp_` are *temporary*: they appear only under the **Temporary** filter, and
once such a project has at most one transcript, 14 days of inactivity and a
folder that no longer exists, it is moved to `~/.claude/projects-archive/`
at startup (move it back to restore). Other machines' sessions appear only
with Settings → Behavior → *Show other machines' sessions* on and
`HELIOS_SESSION_POOL` pointing at a session pool root; they are read-only.

**Status dots.** Every row carries a dot: working, complete (the last turn
finished, whether or not the process is still open), stuck (your last message
got no reply and nothing is running), errored, and a distinct *needs you* dot
when a background session is waiting on an answer or an approval. That dialog
is not raised over the chat you are in: the row's dot changes, a desktop
notification is posted, and the dialog opens when you switch to that session.

**Archive.** Local sessions whose transcript is older than four days are moved
(never deleted) from `~/.claude/projects/<encoded-cwd>/` to
`~/.helios/session-archive/<encoded-cwd>/<id>.jsonl`, 15 s after launch and
every 24 h, at most 8 per run; a session with a live agent process is skipped.
Claude Code's own `claude --resume` no longer sees an archived session; `mv`
the file back into `~/.claude/projects/<encoded-cwd>/` to restore it.

**Titles.** Settings → Behavior → *Title Generation Backend* defaults to
*First message (no model)*. Optional choices are *Ollama* (a separately
running server and configured model), *Built-in Local Engine* (an installed
`llama-cli`; the selected GGUF model can be downloaded if absent), and
*Claude* (an extra CLI model request consuming the active account or API
allowance, with the supported $0.05 per-title CLI guard). Subscription usage
is not necessarily a separate dollar charge. Unknown backend values also
make no model call. Right-click a row → *Rename…* sets a title by hand.

**Starting a chat.** Pick a model in the toolbar, choose a folder, type,
and send with `Ctrl+Enter`. `Enter` is a newline. Typing `/` lists Helios's
agent commands and, in Claude chats, the CLI's own commands and skills with
descriptions; the attach button inserts a file as `@path`. While an agent
works you can keep typing — the message queues and goes when the turn ends.
`Esc` or `Ctrl+.` stops the current turn; queued messages come back to the
composer.

**Chat in a worktree.** The new-chat menu's *Chat in a Worktree* creates a
git worktree of the current chat's repository (else the default folder's) on
a new `helios/<repo>-<time>` branch under `~/.helios/worktrees/`, and starts
the chat there, so its edits never touch your main checkout. Commit, push and
open a PR from the Changes pane; *Remove worktree* there deletes the folder
once everything is committed and keeps the branch. Nothing is redirected into
a worktree unless you pick this.

**Rewind and fork from a message.** Each of your saved messages has a ⋮
menu. *Rewind code to before this message* opens Rewind on the checkpoint
taken just before it was sent. *Fork conversation from here* (Claude chats)
copies the conversation up to that message into a new chat — the original is
untouched — opens it, and puts the message back in the composer to edit and
resend. GPT chats fork at the end from the command palette.

**Sending images.** Paste an image (`Ctrl+V` with a picture on the
clipboard), drop an image file, or press the camera button to take a
screenshot through the desktop portal (pick a window or an area). Each becomes
an `@path` reference in your message — pasted images are saved under
`~/.helios/media/` — and appears as a thumbnail above the text. Claude, GPT,
Muse and Kimi receive the image itself, not just the path (images over 5 MB stay a
path); Gemini and OpenRouter get the path as text.

**Browser pane for agents.** Besides opening, reading and screenshotting
pages, agents can click (CSS selector or a link/button's text), type into
fields (optionally submitting) and read the page's console and uncaught
errors — all under the same per-site consent. The camera button in the
browser toolbar attaches a screenshot of the page to your message.

**Images and files.** Images in a conversation — screenshots the browser pane
returns, an image a tool read, a picture the model generated — show as
thumbnails under the message. Click one to open it full size in the side pane
(the browser pane, which also renders PDFs and HTML); right-click copies it.
Inline images are kept once under `~/.helios/media/` and saved conversations
point at that file. A tool card that touched a file (Read, Write, Edit) has an
open button that shows the file in the side pane.

## Permissions

Google requires a project folder and offers distinct **Sandboxed**,
**Accept edits**, **Bypass** and **Read only** modes. Its headless integration
cannot show or answer interactive approval prompts; sandboxed modes may
still permit workspace writes. Read
[Google permission modes](google-subscription.md#permission-modes-and-effort-levels)
for the native policy behind each choice.

Permissions are the third value of the **Execution** capsule beside the model
picker in the chat toolbar (`Default · High · Ask` = workflow · reasoning ·
permissions), set per conversation. The default for new conversations is in
Settings → Defaults.

- **Workflow** is *Default* for every provider, and additionally *Plan* for
  GPT when the Codex App Server advertises native plan mode. The Plan
  workflow forces read-only permissions for that thread.
- **Reasoning** follows the provider and model's advertised capabilities.
  Available levels, defaults and whether reasoning can be disabled differ.
  Codex uses App Server's levels; OpenRouter preserves its model's exact
  supported levels and mandatory-reasoning policy. Controls can be absent
  when the model explicitly does not support them.
- **Permissions** use the stored Helios identifiers below; these are not
  literal CLI arguments. The table covers Claude, Codex and OpenRouter.

| Mode | Key | Claude | GPT (Codex) | OpenRouter |
|---|---|---|---|---|
| **Ask** | `default` | Every tool that needs approval asks | Workspace-write sandbox, network off; asks before broader or unsandboxed access | In-folder reads run; everything else asks |
| **Accept edits** | `acceptEdits` | File edits are approved automatically; commands still ask | Workspace-write sandbox with network; in-workspace edits run unprompted, escalation still asks | In-folder reads and edits run; commands and paths outside the folder ask |
| **Auto** | `auto` | Guarded automation; prompts only for escalation | As Accept edits | As Accept edits |
| **Bypass** | `bypassPermissions` | No approval prompts, unsandboxed | Full access, no prompts | Everything runs |
| **Plan** | `plan` | Read-only investigation; Claude submits a plan for approval | Read-only sandbox, network off | In-folder reads run; everything else is refused |
| **Never ask** | `dontAsk` | Stays workspace-scoped; refuses anything that would need escalation | Workspace-write sandbox, network off; refuses escalation | As Plan |

Where the Codex network is off, a command that needs it fails inside the
sandbox: in Ask that raises an approval prompt, in Plan and Never ask it is
refused.

Kimi maps both **Never ask** and **Bypass** to its native YOLO mode; Never ask
must not be treated as a restricted refusal mode there. Muse maps permissions
through MSP and establishes sandbox posture when its process starts. Google
uses the distinct modes described above. Read each provider's native policy
before assuming the same label creates the same isolation.

Two rules hold whatever you pick:

- A conversation whose working directory is your **home folder is
  read-only** (Plan). The picker tells you when it has narrowed a choice.
- **Bypass is unsandboxed.** Helios scrubs unrelated and cross-provider
  credentials from the agent's environment, but your files, file-backed
  secrets and a forwarded SSH agent are not sandboxed. Use it in a checkout
  you can afford to have rewritten.

**The approval dialog** shows what the CLI itself reported: the tool, its
description, the full command or the unified diff, and the rule the CLI would
remember. *Allow once* approves this request. *Allow for this session* adds
that rule (for example `Bash(python3 -m pytest:*)`) until this conversation's
agent process exits — nothing is written to disk. *Allow and auto-accept
edits* (offered for file writes) approves and switches this conversation to
Accept edits. *Deny* stops it, and *Other* lets you type why, which the agent
reads.

**Plan review.** In Plan mode Claude investigates read-only, writes its plan
and submits it. Helios renders the plan and offers *approve*, *approve with
edits auto-accepted*, or *keep planning* with your feedback. Approving
switches the conversation out of Plan; the toolbar and the saved settings
follow.

**Questions.** When an agent asks a question (Claude's `AskUserQuestion`,
Codex's one-to-three questions) it arrives as an explicit dialog with the
options the agent offered plus a free-text answer. A subagent's question names
the subagent that asked.

## Budgets

The metered Claude and GPT paths and the OpenRouter runtime have the
breakers below. A trip marks the Work budget-limited, asks the live agent to
cancel, and keeps queued text in the composer. These are provider-specific
execution controls, not a cap on your account across other applications or
a guarantee that a provider has stopped spending before cancellation settles.
Reopen a budget-limited Work from the app menu → *Clear budget block*; it is a menu choice on purpose, with no shortcut.

| Provider | Breaker | Default |
|---|---|---|
| Claude | The CLI's own `--max-budget-usd`, per process, covering subagents the process spawns | $10 on API (per-token) billing; none on a subscription, where the CLI's cost figure is an estimate rather than money |
| GPT | Aggregate token budget across the root thread and its subagents, measured by the App Server | 200,000 tokens on API-key billing; none on a ChatGPT subscription |
| OpenRouter | A per-Work dollar allowance reserved before every request, and a tool-round ceiling | $5 per Work · 25 tool rounds per turn, extended up to 100 while the turn stays productive; an endpoint that publishes no price is priced from the model's catalog row (a warning is logged once at session start); a model with no usable price anywhere is refused before any request is sent — "OpenRouter could not verify this model's prices"; repeated identical tool rounds pause with a resumable handoff |

These limits do not establish an equivalent dollar or token cap for Google,
Muse or Kimi. Subscription quota and usage reporting remain the provider
CLI's responsibility. None of the limits above is configurable: there is no
setting, file key or environment variable for them. The only lever is which billing mode the CLI
is signed in with: an exported `ANTHROPIC_API_KEY` or `CODEX_API_KEY` in
Helios's environment counts as per-token billing even when a subscription
login exists. On per-token Claude billing the cap requires Claude Code
2.1.217 or newer advertising `--max-budget-usd`; an older CLI refuses to
start a chat (see [Troubleshooting](#troubleshooting)).

The context meter in the toolbar shows how full the model's window is and
what fills it (system prompt, tools, memory files, messages); its *Compact
now* button runs the provider's native compaction. Claude sessions also
auto-compact, and Helios shows the boundary in the transcript when that
happens. Live rate-limit details for Claude and Codex are in the same
toolbar.

## Works, goals and plans

A **Work** is Helios's unit of durable work: one objective, one definition of
done, one acceptance checklist, owned by you and shared by whichever provider
is executing. Set or edit it from the app menu → *Set a goal…* or from the
goal strip between the transcript and the composer; pause, resume and
complete it there. The
model's own plan — Claude's task list, Codex's native plan — is tracked
separately as *the execution plan* and drives the `X/Y tasks complete` chip;
it can never rewrite the goal you accepted.

The **workspace pane** (header, right) switches between:

- **Plan** — the execution plan, the tasks done, and quality signals such as
  whether a passing check actually covered the files that changed.
- **Changes** — review the repository's current changes (from every
  session, not just this one). Each file's diff is split into hunks under
  *Staged* and *Unstaged*: stage, unstage or discard a hunk or the whole file
  (discards ask first; an untracked file goes to the trash, never deleted).
  The comment button on a hunk collects notes, and *Send N comments to agent*
  delivers them to the chat you are viewing as one message. The bottom bar
  commits the staged changes, pushes the branch (setting its upstream the
  first time), and opens a pull/merge request with `gh` or `glab` in a
  terminal.
- **Terminal** — your own shell, in tabs, starting in the chat's folder
  (needs `gir1.2-vte-3.91`). Select output and press *Send to chat* to put it
  in the message box as a code block.
  Its *Run* menu lists the project's run configurations — `.claude/launch.json`
  (the file Claude desktop's Preview uses: `name`, `runtimeExecutable`,
  `runtimeArgs`, `port`, optional `cwd` and `env`), else `package.json`'s
  `dev`/`start` script — and starts one in its own tab; when its `port`
  answers, the page opens in the browser pane.
- **Background** — shells, monitors and agents a Claude chat left running
  between turns, each with *Stop* and, once it has written some, *Output*
  (opens in the side pane). Helios keeps such a session alive until they end.
- **Capabilities** — what the connected provider reported for this
  conversation: the tools it has, its MCP servers, and the instruction files
  it loaded.
- **Claude memory** — the global and project `CLAUDE.md`, the memory index,
  and a memory-file editor with atomic saves and rotating backups under
  `~/.helios/backups/`.
- **Missions** and **Shared** — optional integrations. Missions lists
  *tandem* missions found under `$TANDEM_STATE_DIR` (default `~/.tandem`) and
  is empty without the tandem engine; Shared reads a scratchpad service that
  is not part of Helios and reports "can't reach the scratchpad" when there
  is none. Neither affects chats.

**Rewind.** In a folder that is a git repository (`git` installed), Helios
snapshots the worktree as a dangling git tree before each message you send —
your index, refs and branches are untouched, and git-ignored files are not
captured. App menu → *Rewind files…* restores one; files a restore removes go
to `$HELIOS_STATE_DIR/checkpoint-trash/`, never deleted. Outside a
repository the dialog says "No checkpoints yet"; when the snapshot takes
longer than 4 s the message is sent without one and a notice says that rewind
is unavailable for it.

**The Agent Dock.** When the active provider reports subagents for the
current turn, a dock appears in the conversation with one node per agent —
what it is doing, which one needs you — and, in Claude chats, a stop button
per agent that stops that subagent without interrupting the session.

## Keyboard shortcuts

| Keys | Action |
|---|---|
| `Ctrl+Enter` | Send the message |
| `Esc` / `Ctrl+.` | Stop the current turn (queued messages return to the composer) |
| `Ctrl+Shift+.` | Emergency stop: every session, and the shared Codex transport |
| `Ctrl+N` | New chat in the default folder |
| `Ctrl+Shift+N` | New chat — choose the folder |
| `Ctrl+F` | Search all transcripts |
| `Ctrl+L` | Focus the composer |
| `Ctrl+Shift+P` | Agent commands: compact, review, fork, revert |
| `/` in the composer | Helios's agent commands; in Claude chats also the CLI's commands and skills |
| `Ctrl+1` | Toggle the sessions sidebar |
| `Ctrl+3` | Toggle the workspace pane |
| `Ctrl+R` | Reload sessions from disk |
| `Ctrl+Q` | Quit |

Typed `/model <alias>` and `/effort <level>` work in Claude chats and route
through the toolbar handlers: `/model` changes the sticky default model
(applied to the live process when the CLI allows it, otherwise at your next
chat), `/effort` sets this conversation's reasoning level.

## Settings

**Accounts.** One row per provider — Claude, GPT, Gemini, Muse, Kimi and
OpenRouter — showing the account identity and plan where its runtime reports
them. Native providers use their own authentication checks. *Sign in…*, *Sign out*
and *Switch account…* open that CLI's own flow in a terminal (Gemini opens
`agy`, whose `/logout` and `/login` do the switch); the page re-reads every
account when you return, and a changed account refreshes the model pickers.
Kimi uses `kimi login` and owns its membership credentials. OpenRouter is
an API key, so its row opens *Providers*; that key is stored in
`~/.helios/openrouter.key`. Native CLI credentials are not copied by Helios.

**Settings.** *Defaults* — permission mode and
model for new chats (persisted only by the **Save chat defaults** button at
the bottom of the page) and the default working directory (saved as soon as
you choose it). *Appearance* — colour scheme (auto, light, dark) and a
translucent sessions sidebar. *Behavior* — notifications when a background
session finishes or a tandem mission is gated, the four-choice title backend
(First message is the default), and other machines' sessions. Optional
Ollama server/model edits are persisted by *Save and check*.
*Diagnostics* — the log file.

**Providers.** Each native provider has discovery and account information
under Settings → Providers. Claude shows the resolved `claude` binary;
Codex uses its App Server; Google uses Antigravity; Muse uses MSP; and Kimi
uses ACP. Refresh the provider to check the models and capabilities the
connected runtime reports. A catalogue row is not proof that an account is
signed in or entitled to use it.

OpenRouter's API key is stored at `~/.helios/openrouter.key` (mode 0600).
The provider page fetches its model catalogue and lets you choose which
models appear in the picker; an empty selection means the whole catalogue.
Models without tool support are marked accordingly. The optional Smart
Routing broker remains unavailable unless its service is installed.

**Tools.** *Helios Router* (the optional broker above), *Claude MCP servers*
(live, from `claude mcp list`, with health), *Codex MCP servers* (the
inventory captured by your last GPT chat — empty until one has run), and
*Built-in tools* (the tool set your last Claude session reported).

### Settings keys

Every control above is a key in `~/.helios/ui-state.json`: flat JSON, mode
0600, missing keys take their defaults. The file is read once at start and
rewritten whole on every change, so edit it only while Helios is closed.

| Control | Key | Values |
|---|---|---|
| Defaults → Default permissions | `permission_mode` | `default` · `acceptEdits` · `auto` · `bypassPermissions` · `plan` · `dontAsk` (default `default`); `bypassPermissions` is honoured only with `permission_mode_confirmed: true`, otherwise it is clamped to `default` |
| Defaults → Model | `model` | model id (default `fable[1m]`); an empty value is read back as `fable[1m]` at the next launch, so *Default (from Claude settings)* in the picker lasts only for the running session |
| Defaults → Default working directory | `default_cwd` | absolute path; unset, or no longer a directory → the most recent project folder Helios knows about, else `$HOME` (read-only) |
| Appearance → Theme | `color_scheme` | `auto` · `light` · `dark` |
| Appearance → Translucent sessions sidebar | `glass_sidebar` | `true` · `false` |
| Behavior → Notify when a background session finishes | `notify_on_finish` | `true` (default) · `false` |
| Behavior → Notify when a tandem mission is gated | `notify_on_mission_gate` | `true` (default) · `false` |
| Behavior → Title Generation Backend | `title_backend` | `first_message` (default, no model) · `ollama` · `builtin` · `claude`; unknown values make no model call |
| Behavior → Built-in Model | `builtin_title_model` | `qwen2.5-0.5b` (default) · `llama-3.2-1b` · `phi-3-mini`; requires installed `llama-cli` |
| Behavior → Ollama server URL | `ollama_url` | default `http://localhost:11434` |
| Behavior → Title model | `ollama_title_model` | default `qwen2.5-coder:14b` |
| Behavior → Show other machines' sessions | `show_pool_sessions` | `true` · `false` (default) |
| Providers → Models in the picker | `openrouter_picker_models` | list of model ids; empty = whole catalog |
| Execution capsule → Reasoning (global default) | `effort_level` | `off` · `low` · `medium` · `high` (default) · `xhigh` · `max` |

Per-conversation overrides live in `$HELIOS_STATE_DIR/conversation-perms.json`
as `{provider: {session_id: {permission_mode, effort_key?, workflow_mode?}}}`
with provider `anthropic`, `openai`, `google`, `muse`, `kimi` or `openrouter`. The full table, with the
files each key lives in, is in [agent-setup.md](agent-setup.md).

## Environment variables

Paths written as `~/.helios` are the default app-state location. A configured `HELIOS_STATE_DIR` relocates app-owned paths; native provider data remains separate.

| Variable | Effect |
|---|---|
| `HELIOS_CLAUDE_BINARY` | Path of the `claude` binary to use, ahead of `PATH`; must be an executable file, otherwise it is silently skipped |
| `HELIOS_CODEX_BINARY` | Path of the `codex` binary, ahead of `PATH` and `~/.local/bin/codex` |
| `HELIOS_STATE_DIR` | Helios app-state root (default `~/.helios`), including UI settings, keys/catalogues, title/project caches, archives, backups, media, browser state, managed worktrees, checkpoints, goals and Work data. Native provider stores, Claude projects/memory, tandem state and the title-generation working cache can remain elsewhere; this variable alone does not isolate a demonstration environment |
| `HELIOS_DEBUG` | Any non-empty value: everything at DEBUG, including the Claude driver's spawn argv, the type of every record the CLI emits, its stderr and its exit code; overrides `HELIOS_LOG_LEVEL` |
| `HELIOS_LOG_LEVEL` | Python logging level name (`DEBUG` … `CRITICAL`); default `INFO`; an unknown name is treated as `INFO`. `WARNING` quiets startup |
| `HELIOS_CODEX_TRANSPORT` | Leave unset; `exec` refuses every GPT chat |
| `HELIOS_ROUTER_SOCKET` | Unix socket of an optional Smart Routing broker (default `/run/helios-router/router.sock`); inert unless the socket answers |
| `HELIOS_SESSION_POOL` | Root of an optional read-only session pool shared between machines (default `~/.local/share/helios/session-pool`); ignored when the path does not exist |
| `HELIOS_SCRATCHPAD_URL`, `HELIOS_SCRATCHPAD_KEY` | Endpoint (default `http://127.0.0.1:9101`) and optional API key of the scratchpad service the Shared pane reads |
| `HELIOS_TANDEM_BINARY` | Path of the optional `tandem` binary for the Missions pane |
| `CLAUDE_HOME` | Where Claude Code transcripts are read (default `~/.claude`) |
| `XDG_DATA_HOME` | Tool inventories cached under `<dir>/helios/` (default `~/.local/share`) |
| `XDG_CONFIG_HOME` | Optional extra instructions: every `*.md` file in `<dir>/helios/system-prompt.d/` (default `~/.config`), in name order, is added to every session's instructions; read at startup |
| `TANDEM_STATE_DIR` | Tandem mission state for the Missions pane (default `~/.tandem`) |
| `TERMINAL` | Terminal emulator for the Settings → Accounts *Sign in…*, *Sign out* and *Switch account…* buttons (else the first of `x-terminal-emulator`, `gnome-terminal`, `kgx`, `konsole`, `tilix`, `kitty`, `alacritty`, `foot`, `xterm`) |

A desktop launch does not read `~/.bashrc`; set these on the desktop entry's
`Exec=` line (`Exec=env HELIOS_CLAUDE_BINARY=/path/to/claude helios`).
`HELIOS_DEBUG`, `HELIOS_LOG_LEVEL`, `HELIOS_CLAUDE_BINARY`,
`HELIOS_CODEX_BINARY`, `HELIOS_CODEX_TRANSPORT`, `HELIOS_ROUTER_SOCKET`,
`HELIOS_TANDEM_BINARY` and the two `HELIOS_SCRATCHPAD_*` variables are
stripped from the agents' own environment;
`HELIOS_STATE_DIR` and `HELIOS_SESSION_POOL` pass through.

**Credentials are not configured through Helios variables.** `claude` sign-in
is its own: `claude auth login`, or `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in Helios's environment,
which are the only auth variables forwarded to Claude — an exported
`ANTHROPIC_API_KEY` also makes Helios treat the account as per-token and
apply the $10 cap. `OPENAI_API_KEY` is not read and not forwarded: sign in
with `codex login`, or paste a key in Settings → Providers, which runs
`codex login --with-api-key`. There is no OpenRouter environment variable:
the key is read only from `~/.helios/openrouter.key` (one line, at least 16
characters, no whitespace, mode 0600); write that file while Helios is closed
or paste the key in Settings. Every other variable whose name contains
`TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `_PASS`, `CREDENTIAL`,
`APIKEY`, `_KEY`, `_PWD`, `_PAT`, `JWT`, `AUTH_CONFIG`, `ASKPASS` or
`KEYRING` is stripped from the agents' environment; `SSH_AUTH_SOCK` is kept.
Two credential-shaped names are forwarded on purpose when set, for optional
integrations that are inert without them: `NORVI_TRACKER_API_TOKEN` (an
optional work-tracker token) to Claude, GPT and OpenRouter, and
`INFISICAL_CLIENT_SECRET` (an optional secrets-manager identity) to Claude
only. Leave both unset if you do not use those services.

## Troubleshooting

**Settings says "Not found — set $HELIOS_CLAUDE_BINARY".** Helios looks at
`$HELIOS_CLAUDE_BINARY` (must be an executable file, otherwise it is silently
ignored), then `PATH`, `~/.local/bin/claude`, the newest
`~/.local/share/claude/versions/*`, then VS Code and JetBrains bundles — the
badge beside the path in Settings → Providers → Anthropic · Claude says which one won. A desktop
session has a shorter `PATH` than your terminal: symlink the binary into
`~/.local/bin`, or set the variable on the desktop entry
(`Exec=env HELIOS_CLAUDE_BINARY=/path/to/claude helios`).

**The startup message says GTK, libadwaita or GtkSourceView is too old.**
Helios needs GTK 4.10, libadwaita 1.5 and GtkSourceView 5; Ubuntu 24.04 and
newer have them. The check runs before the window opens: the message is
printed to stderr, shown in a dialog when GTK can display one, and the
process exits with status 1. It names the packages to upgrade
(`gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-gtksource-5`).

**`ImportError: cannot import name 'UTC' from 'datetime'` (or a similar
`StrEnum`/`tomllib` import error) at launch.** Python is older than 3.11;
the startup check covers only the GTK libraries.

**A control is missing — no effort slider, no Claude spend cap.** The
connected CLI is older than the feature. At startup a toast reads "Claude CLI
<version> cannot do: …" and the log carries a `claude CLI <version>;
degraded: …` line naming the missing flag (`--effort` = the reasoning
slider, `--max-budget-usd` = Claude's spend cap, `--forward-subagent-text` =
live subagent text in the Agent Dock). Update the CLI.

**"Claude's required process-family budget breaker is unavailable" when
sending.** You are on per-token billing and the `claude` CLI is older than
2.1.217 or lacks `--max-budget-usd`; update Claude Code.

**GPT toggle disabled, Settings says "Codex CLI not available".** Install the
CLI (`npm install -g --prefix ~/.local @openai/codex`) or set
`HELIOS_CODEX_BINARY`; Helios looks at that variable, `PATH`, then
`~/.local/bin/codex`.

**GPT shows "Not signed in".** Run `codex login` in a terminal (ChatGPT
account), or paste an OpenAI API key in Settings → Providers → *OpenAI ·
Codex* — Helios hands it to `codex login --with-api-key` on stdin and keeps
no copy. Sign-in state comes from `codex login status`; Helios never reads
`~/.codex/auth.json`.

**Where is the log?** Settings → Diagnostics, or
`$HELIOS_STATE_DIR/logs/helios.log` (default `~/.helios/logs/helios.log`;
rotates at 1 MB, three backups). `HELIOS_DEBUG=1 helios` from a terminal
(`HELIOS_DEBUG=1 ./scripts/helios` from a checkout) adds the Claude CLI's
spawn argv, the type of every record it emits, its stderr and its exit code,
which is what a bug report needs. To check a start:

```bash
grep 'claude CLI' ~/.helios/logs/helios.log | tail -n 1
```

A healthy start prints one line ending `helios.window: claude CLI <version>;
degraded: nothing`; anything after `degraded:` names a feature the installed
CLI cannot provide.
