# Helios quickstart: your first coding-agent session on Linux

Helios brings coding agents, project conversations, a browser, terminal and
Git review into one native Linux window. This guide gets you from a fresh
installation to a small task whose result you can check.

## 1. Install Helios

The packaged build is tested on **Ubuntu 26.04**. The APT service supports
**amd64**; other distributions and architectures should use the
[source instructions](../README.md#other-distributions--run-from-source).
The application needs Python 3.11+, GTK 4.10+, libadwaita 1.5+ and
GtkSourceView 5. The package installs the browser dependency; leave APT's
recommended packages enabled to include the terminal and screenshot portal.

For the latest features, download the current release package and its
checksums into a new directory:

```bash
mkdir -p ~/Downloads/helios-0.114.4 &&
cd ~/Downloads/helios-0.114.4 &&
curl -fLO https://github.com/spencernorton/helios/releases/download/v0.114.4/helios_0.114.4_all.deb &&
curl -fLO https://github.com/spencernorton/helios/releases/download/v0.114.4/SHA256SUMS.txt &&
grep '  helios_0.114.4_all.deb$' SHA256SUMS.txt | sha256sum --check - &&
sudo apt install ./helios_0.114.4_all.deb &&
helios
```

Expected checksum result: `helios_0.114.4_all.deb: OK`. Stop if verification
fails. The release also includes its matching source archive. Check
[the latest release](https://github.com/spencernorton/helios/releases/latest)
when choosing a newer version; use the package and checksums from the same
release.

Helios appears in your application launcher. The `helios` command opens the
same window; a second invocation activates the running instance.

### Choose how updates arrive

The [stable APT setup](../README.md#ubuntu-2604--stable-apt-updates) configures
normal system updates. **Stable and the latest release are different
channels.** On 29 October 2026, stable APT contains 0.101.0 and the separate
opt-in candidate channel contains 0.114.4. The features in this guide require
the newer release. Installing its downloaded package does not automatically
enrol your machine in candidate updates.

`apt-cache policy helios` shows the versions available from your configured
sources. Get subsequent release packages from GitHub, or use the
[installation guide](https://norvitech.com/helios/install/) to choose your
update path. Do not change the suite owned by the archive keyring package to
try to enable candidates.

## 2. Connect one provider

Install and sign in to **one** agent first. You can add the others later.
Helios does not include the agent CLIs, provider subscriptions or API credit.

| Provider | What to do before chatting |
|---|---|
| Claude Code | Install the official `claude` CLI; run `claude auth login --claudeai` for your Claude account, or configure its supported API authentication. |
| OpenAI Codex / GPT | Install the official `codex` CLI; run `codex login` for ChatGPT. Settings → Providers also accepts a Codex API key. |
| Google / Gemini | Install Antigravity CLI, open `agy` and complete Google-account sign-in. Follow [Google setup](google-subscription.md). |
| Muse Code | Install `muse`, then `muse login` for your Meta account; a Meta API key is an optional alternative in Settings → Providers. |
| Kimi Code | Install Kimi Code 2.3.2+, then `kimi login` for your membership. Follow [Kimi setup](kimi-subscription.md). |
| OpenRouter | Enter an API key in Settings → Providers; Helios supplies the runtime. |

Open **Settings → Accounts** to check your sign-in, then **Settings →
Providers** to refresh the model list. Each native CLI owns its credentials;
Helios stores an OpenRouter key only if you supply one. Google uses
Antigravity's account route, and Kimi uses native membership authentication.

If a CLI works in a terminal but Helios cannot find it, your application
launcher may have a shorter `PATH`. The
[user guide](user-guide.md#troubleshooting) explains binary overrides and
`~/.local/bin` discovery.

## 3. Pick the project folder

Press **Ctrl+Shift+N**, then choose the folder you want the agent to work in.
For future chats you can set **Settings → Defaults → Default working
directory**.

A fresh installation otherwise falls back to your home folder, where Helios
forces read-only operation. Google sessions require a project folder. In a
Git project you can choose **Chat in a Worktree** from the new-chat menu to
keep its file edits in a separate checkout. A worktree separates files; it
shares Git history and does not isolate credentials or external services.

## 4. Send a small first task

Choose an available model in the composer toolbar. Open **Execution** to
review the permission mode and supported reasoning effort. These choices
vary by provider; Google's **Sandboxed** mode cannot display or answer
interactive CLI approval requests. Choose a read-only mode for an
explanation, or an editing mode when you want changes.

Try a bounded request:

> Read the README and the main source entry point. Explain how this project
> starts, cite the files you used, and suggest one small improvement. Do not
> change files or run commands.

Send with **Ctrl+Enter**. Enter inserts a newline. The transcript shows the
answer and tool activity; **Esc** or **Ctrl+.** stops the current turn.

## 5. Review a change

For an editing task, name the intended file and behaviour, then open the
**Changes** workspace pane. Read the diff, comment on a hunk and send the
comment back to the agent, or stage only the changes you want. The pane can
commit, push and open a pull request using your installed Git hosting CLI.
Its diff includes all changes in that checkout, including changes from
other sessions.

Open the **Browser** pane with **Ctrl+Shift+B** to preview local reports or
a dev server. The **Terminal** pane gives you your own shell in the project's
folder and can send selected output to the composer without submitting it.

In a Git repository, **Rewind files…** can restore a pre-message checkpoint.
Ignored files, external actions and messages without a completed checkpoint
are outside its coverage. Check the diff after restoring.

## Keep exploring

- [Feature walkthrough](https://norvitech.com/helios/features/)
- [Provider setup and capability differences](https://norvitech.com/helios/providers/)
- [Narrated demos, captions and transcripts](https://norvitech.com/helios/demos/)
- [User guide](user-guide.md) for goals, context, permissions, usage and shortcuts
- [Security and data handling](../SECURITY.md)
- [Troubleshooting](user-guide.md#troubleshooting)
