# Helios parity roadmap — October 2026

Benchmark date: 2026-10-22. Baseline: Helios 0.105.3 (`main`), plus the two
unmerged branches `claude/browser-pane` and `claude/usage-stats`.

## Benchmark set

First-party apps:

- **Claude desktop Code tab.** Linux has been a beta since 2026-06-30. The
  benchmark also covers the VS Code and JetBrains extensions and
  claude.ai/code.
- **OpenAI Codex app.** It merged into ChatGPT desktop on 2026-07-09. Linux has
  been a preview since 2026-08-11. The benchmark also covers the IDE extension
  and Codex cloud.
- **Google Antigravity 2.0.** This covers the App, the IDE and the `agy` CLI.
  Gemini CLI and Code Assist were cut off for consumers on 2026-06-18.

Third-party apps: Cursor 3 (Agents Window), Devin Desktop (formerly
Windsurf), Zed 1.25, VS Code with Copilot (agents window and harnesses), Warp,
Conductor and Cline. Roo Code shut down on 2026-05-15.

Sources are the vendors' current docs and changelogs, collected on 2026-10-22.
Claims about Conductor come from the vendor only.

## Where Helios already leads

- **More providers in one native GUI.** Helios drives Claude, Codex, Gemini
  (agy), Muse and OpenRouter. Only VS Code and Zed host several vendors' agents
  side by side, and neither covers OpenRouter or Muse.
- **Several providers on one task.** Works, tandem missions and per-provider
  participants share one logical task. Durable admission, $ / token breakers
  and Emergency Stop All govern execution.
- **One account view.** The Accounts page covers every provider and switches
  accounts in each CLI.
- **Native Linux.** The Claude desktop app is a Linux beta without computer
  use or voice, and the Codex app is a Linux preview.

## Gap matrix

| Capability | Benchmark state (examples) | Helios today | Gap |
|---|---|---|---|
| See images an agent reads or produces | Codex lightbox and Canvas; Antigravity media viewer; Zed and VS Code inline | Dropped: the transcript parser keeps text only | **Critical** |
| Open files, PDFs and HTML from the chat | Claude browser pane and Files pane; Codex artifact viewer | None | **High** |
| Image input (paste, drop, screenshot) | All of them | `@path` text only; no clipboard images | **High** |
| In-app browser and dev-server preview | Claude, Codex, Cursor, Antigravity, VS Code | Branch only, with 5 tools and no click or type | **High** (land and extend) |
| Point-and-prompt on the running app | Cursor Design Mode, VS Code "Add to Chat", Conductor, Devin | None | Medium |
| Review pane: per-hunk stage or revert, commit, PR | Codex, Zed, Claude, Conductor, Antigravity | Read-only Changes pane | **High** |
| Inline diff comments sent back to the agent | Claude, VS Code, Warp, Conductor, Antigravity | None | **High** |
| Checkpoint timeline: rewind or fork from any message | Claude VS Code, Cursor, VS Code, Zed | Rewind dialog; fork for GPT only | Medium |
| Integrated terminal | All of them | None (no VTE) | **High** |
| Worktree per session, run in parallel | All of them | Removed in v0.66 (the lease) | Medium (opt-in) |
| Multi-session board or split view | Devin Command Center, VS Code grid, Conductor, Claude split view | Sidebar only | Medium |
| Background-tasks pane (shells, monitors) | Claude desktop | Agent dock covers subagents only | Medium |
| Cost and usage per session | VS Code, Devin, Warp `/cost`, Cline | Branch only | Medium (land) |
| Export and share a session | Zed, VS Code, Warp, Cursor, Codex | None | Low |
| MCP, plugin, skill and hook management UI | All of them | Read-only lists | Medium |
| Editable plan with comments before execution | Cursor, VS Code, Devin, Warp, Antigravity | Plan pane is read-only | Medium |
| Voice input | Codex, Antigravity, Cursor, Warp, Cline | None | Low |
| Remote control from a phone, with push | Claude, Codex, Cursor, Warp, Antigravity, Conductor | None | Medium (estate fit) |
| Cloud VMs and computer use | Claude, Codex, Cursor, Devin, Warp | None | Deferred |

## Phases

Each phase ships as patch or minor releases, follows the existing workflow
(worktree, tests, PR, tag, `.deb`), and is checked in the real window on the
Xvfb rig before release.

### Phase 0: land what is already built (S)

1. Rebase `claude/usage-stats` and `claude/browser-pane` onto `main`. Renumber
   both: each claims 0.105.0 / 0.106.0, and 0.105.0 has already shipped.
   Resolve the overlap with 0.105.2's Muse meter fix. Review both, then
   release.
2. Make the untracked `AGENTS.md` in the canonical checkout match `main`. It
   describes the browser pane as shipped.

### Phase 1: see what the agent sees (M). This answers the first complaint.

1. **Image blocks in the transcript.** The parser keeps `image` content from
   four sources: Claude tool results and user turns, Codex
   `imageView` / `imageGeneration`, MCP image content (including browser
   screenshots), and Muse items. Bubbles show thumbnails using `Gtk.Picture`
   from a `Gdk.Texture`. Large images are written to
   `~/.helios/media/<sha>.png` and are never inlined into the persisted
   history.
2. **Viewer pane.** It is a new workspace-pane entry beside Plan, Changes and
   the rest. Clicking a thumbnail opens it with fit, zoom, copy, save and
   "open externally" actions. Clicking a file path in the transcript opens the
   file there:
   - Images.
   - PDFs, rendered with Poppler (`gir1.2-poppler-0.18`).
   - HTML, rendered in the browser pane.
   - Markdown, rendered.
   - Code, shown in GtkSourceView.
3. **Real image input.** Images reach the composer by clipboard paste
   (`Gdk.Texture`), by drag-and-drop, or through "Attach screenshot" (the
   `Xdp` screenshot portal, which works on Wayland). The composer shows them
   as thumbnails, and each provider receives them natively:
   - Claude: stream-json image blocks.
   - Codex: `localImage` input items.
   - Muse: `ImageInput`.
   - OpenRouter: `image_url` for vision models.
   - agy: a file reference, with the limit stated in the UI.

### Phase 2: a review loop like the benchmark apps (M–L)

4. **Review pane.** It replaces the read-only Changes pane:
   - Views of unstaged, staged and "since this chat started", using the
     checkpoint baseline.
   - Stage or revert per hunk and per file.
   - Commit, with a message the agent can draft.
   - Push, and open a PR through `gh` or `glab`.
5. **Inline diff comments.** You annotate lines, and the comments go to the
   agent as one batched feedback message (Ctrl+Enter).
6. **Checkpoint timeline.** Each user message gets "Rewind code here" and
   "Fork from here". Fork works for every provider:
   - Claude: `--resume --fork-session`.
   - Codex: `thread/fork`.
   - Muse: `session/fork`.
   - OpenRouter: copy the history.
7. **Editable plan.** In Plan workflow, the plan opens as an editable markdown
   document with inline comments. "Approve" executes it.

### Phase 3: execution environment (L)

8. **Terminal pane.** VTE 3.91 (`gir1.2-vte-3.91`, added as a `.deb`
   dependency) gives one tab per session in the session folder. "Send
   selection to chat" attaches output to a message.
9. **Browser, extended.** Adds `browser_click`, `browser_type`,
   `browser_console` and `browser_network`, all behind the existing consent
   scope. "Add to chat" works on an element or a region screenshot, which
   gives point-and-prompt.
10. **Run configs.** Reads `.claude/launch.json` or a Helios `run` entry,
    starts the dev server, and opens it in the browser pane.
11. **Opt-in worktree per session** with merge-back from the review pane. This
    is the user's choice; the removed automatic lease stays removed.
12. **Background-tasks pane.** It lists Claude's
    `background_tasks_changed` tasks: shells, monitors and agents. Each can be
    stopped, and its output file can be opened.

### Phase 4: sessions and visibility (M)

13. **Board view.** All live sessions appear as cards showing state, provider,
    cost and pending approvals. Two transcripts can be shown side by side.
14. **Export** a session to Markdown or JSON, and pin sessions.
15. **Cost and usage** per session and per Work, from the usage-stats branch.
16. **Needs-attention badges** for approvals and questions across sessions.

### Phase 5: extensibility UI (M)

17. **MCP manager.** Add, remove and enable servers per provider, through each
    CLI's own commands and config. OAuth runs in a terminal, like Accounts.
18. **Skills, plugins and hooks browser.** It covers Claude plugins and skills,
    Codex skills and hooks, and agy plugins. It starts read-only, then adds
    enable/disable.

### Phase 6: remote and integrations (L, estate-specific)

19. **Remote control.** A small web UI served only on the tailnet lists
    sessions, sends messages and answers approvals. Push alerts go through the
    existing estate notification path.
20. **Start from a tracker item.** Open a Work from an OpenProject work
    package or a GitLab issue, and post the result back. This ties into the
    Helios Loops packages OP#1046–1049.

### Deferred

- Cloud VMs: Apollo is the execution host.
- Computer use: Wayland limits it, and Claude's Linux beta lacks it too.
- iOS simulator.
- Public share links: they conflict with the estate privacy posture.
- Voice input: low value for now; revisit after Phase 3.

## Sequencing rationale

Phase 1 fixes the complaint that started this review. It also makes the
browser branch useful: until then its screenshots reach the agent but never
the person. Phase 2 closes the gap every benchmark app shares, the "review
what the agent did" loop. Phase 3 needs Phases 0 and 1 because the browser
pane and the viewer pane share an inspector area.

Tracking: one OpenProject work package per phase, created on approval.
