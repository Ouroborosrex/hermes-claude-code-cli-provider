# Core patches (apply to `hermes-agent`, not this plugin)

These implement the Hermes-**core** half of the Codex-parity integration (#9).
Each is **opt-in and default-off** — applying the patch changes no behavior until
you set the config flag.

## `p1-claude-stream-runtime.patch` — P1: opt-in `claude_stream` api_mode
A persistent Claude Code stream-json session as the turn runtime, mirroring the
existing `codex_app_server` integration. New files
(`agent/transports/claude_stream_session.py`, `agent/claude_stream_runtime.py`)
plus ~4 guarded edits (`runtime_provider`, `agent_init`, `conversation_loop`,
`run_agent`). On any failure it degrades to the chat_completions shim.

**Enable:** add `claude_runtime: stream_session` under `model:` in `config.yaml`.

**Apply (from a `hermes-agent` checkout):**
```bash
git checkout -b feat/claude-stream-runtime
git apply core-patches/p1-claude-stream-runtime.patch   # or: git am < ...
```

**Verified live:** flag-off → shim handles turns (shim POST delta +1, no
regression); flag-on → turns route through `claude_stream` (shim POST delta 0,
no fallback). This is the P1 turn round-trip only; event mapping (P2), Hermes
tools/skills over MCP (P3), and the permission/interactivity bridge (P4) follow.
