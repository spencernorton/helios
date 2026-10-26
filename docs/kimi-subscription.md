# Kimi Code membership in Helios

Helios connects to the official Kimi Code CLI over Agent Client Protocol (ACP). This is a native subscription integration: it does not use OpenRouter, a metered API-key fallback, or the Claude-compatible endpoint.

## Install and sign in

Install Kimi Code 2.3.2 or newer and make `kimi` available on `PATH`. Then run:

```bash
kimi login
kimi acp --help
```

Complete Kimi's device-code sign-in in the browser. Kimi owns and refreshes the credential; Helios never reads, copies, or stores it. `HELIOS_KIMI_BINARY` can select a specific executable for a local installation.

Open Helios **Settings → Accounts** to verify the membership or start sign-in/sign-out. **Settings → Providers → Kimi** performs a tool-free ACP handshake and displays the models available to the current account. The picker contains only models returned by that live check.

## What Helios forwards

- Kimi's live model and thinking-level choices, including changes while a session is idle.
- Default, Plan, Auto, and YOLO modes through Helios's per-conversation permission controls. HOME-directory safety restrictions still apply.
- Streaming answers and thinking, plans, tool calls and results, questions, and permission prompts.
- Native first turns, follow-ups, exact session resume, head forks, `/review`, `/compact`, cancellation, and queued messages.
- Native image blocks for supported image attachments and embedded text resources for explicit `@/absolute/path` references. Images retain the shared 5 MB limit; text resources must be UTF-8 and no larger than 1 MiB.
- Kimi-configured MCP servers, the reviewed Helios estate MCP selection, and the Helios browser MCP server. ACP stdio, HTTP, and SSE forwarding remains subject to the installed CLI's advertised capabilities.
- Durable Helios Work receipts and Claude-compatible display transcripts. Kimi's own session store remains authoritative for model context.
- Per-turn token/context usage when Kimi reports it.

The model catalog and thinking options are discovered from ACP rather than hardcoded. Available models, context windows, and quota depend on the membership and the installed CLI. ACP currently exposes no membership quota-window API, so Helios does not invent one or show estimated subscription spend.

## Protocol limits

Kimi ACP does not currently support audio prompts or `elicitation/complete`. Helios does not advertise those as available. Filesystem and terminal execution stay in the Kimi CLI, where its normal tool and approval policy applies; Helios does not claim ACP client-side filesystem or terminal capabilities. Individual subagent cancellation is not exposed by this integration.

Kimi's official protocol references:

- https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp.html
- https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html

## Troubleshooting

- **Kimi Code CLI is not installed:** install `kimi`, or set `HELIOS_KIMI_BINARY` to its executable.
- **Sign-in required:** run `kimi login`, then refresh Settings → Providers.
- **No models available:** run `kimi doctor` and confirm the same account works with `kimi acp`. Helios fails closed rather than inserting fallback model IDs.
- **MCP servers missing:** check `~/.kimi-code/config.toml` and the Settings → Tools inventory. Reviewed estate forwarding requires the existing `~/.helios/estate-mcp.json` selection.
- **Stop or restart:** Helios resumes the same native Kimi session ID. A turn with uncertain delivery remains blocked for provider-backed recovery and is never replayed automatically.
