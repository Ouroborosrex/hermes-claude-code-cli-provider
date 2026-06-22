#!/usr/bin/env python3
"""M1 proof-of-concept (issue #7): persistent multi-turn Claude Code session.

Where M0 proved the *output* event taxonomy from a single `claude -p
--output-format stream-json` call, **M1** proves the *session*: one long-lived
`claude` process driven over the bidirectional stream-json protocol
(`--input-format stream-json --output-format stream-json`), so a host can:

  * send multiple user turns to the SAME process and keep context across them,
  * watch each turn's `tool_use` / `tool_result` / `result` (usage + cost),
  * pin the session to a working directory (per-conversation cwd),

…all on the Claude Code subscription (no API key). This is the primitive a real
Hermes transport would own — the direct analog of `CodexAppServerSession`
(`agent/transports/codex_app_server_session.py`). **Nothing here touches Hermes
core.** See the "Becoming a Hermes transport" note at the bottom.

Run:   python3 m1_stream_session.py
Env:   M1_MODEL (default sonnet), M1_CWD (default: a fresh temp dir),
       CLAUDE_CODE_CLI_BIN (default: autodetect)
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class Turn:
    text: str = ""                       # final assistant text (the `result`)
    tool_uses: list[str] = field(default_factory=list)
    tool_results: int = 0
    usage: dict | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    is_error: bool = False
    event_types: list[str] = field(default_factory=list)


class ClaudeStreamSession:
    """One persistent `claude` process driven over the stream-json protocol.

    Call ``send(text)`` once per user turn; context persists across calls because
    it's the same process. ``close()`` ends the session (closes stdin).
    """

    def __init__(
        self,
        cwd: str | os.PathLike | None = None,
        model: str = "sonnet",
        tools: str = "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,TodoWrite",
        permission_mode: str = "acceptEdits",
        bin_: str | None = None,
    ) -> None:
        self.bin = bin_ or os.environ.get("CLAUDE_CODE_CLI_BIN") or shutil.which("claude") or "/usr/bin/claude"
        self.cwd = str(pathlib.Path(cwd).expanduser()) if cwd else str(pathlib.Path.cwd())
        self.session_id: str | None = None
        argv = [
            self.bin, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",  # required for stream-json output in -p mode
            "--model", model,
            "--permission-mode", permission_mode,
            "--allowedTools", tools,
            "--no-session-persistence",
        ]
        self.proc = subprocess.Popen(
            argv, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

    def send(self, text: str, turn_timeout: float = 300.0) -> Turn:
        """Send one user turn; block until that turn's `result` event."""
        turn = Turn()
        if self.proc.poll() is not None:  # process already exited
            turn.is_error = True
            turn.text = f"[session dead: claude exited {self.proc.returncode}]"
            return turn
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()                         # type: ignore[union-attr]
        except (BrokenPipeError, ValueError):
            turn.is_error = True
            turn.text = "[session dead: stdin closed]"
            return turn

        deadline = time.time() + turn_timeout
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t, sub = ev.get("type"), ev.get("subtype")
            turn.event_types.append(f"{t}/{sub}" if sub else t)
            if t == "system" and sub == "init":
                self.session_id = ev.get("session_id") or self.session_id
            elif t == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        turn.tool_uses.append(b.get("name", "?"))
            elif t == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        turn.tool_results += 1
            elif t == "result":
                turn.text = ev.get("result") or ""
                turn.usage = ev.get("usage")
                turn.cost_usd = ev.get("total_cost_usd")
                turn.session_id = ev.get("session_id") or self.session_id
                turn.is_error = bool(ev.get("is_error"))
                return turn
            if time.time() > deadline:
                turn.is_error = True
                turn.text = f"[turn timed out after {turn_timeout}s]"
                return turn
        # stdout closed without a result → process died mid-turn
        turn.is_error = True
        turn.text = turn.text or "[session ended without a result event]"
        return turn

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def _demo() -> int:
    import tempfile

    model = os.environ.get("M1_MODEL", "sonnet")
    cwd = os.environ.get("M1_CWD") or tempfile.mkdtemp(prefix="m1_session_")
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    print(f"session cwd: {cwd}\nmodel: {model}\n")

    s = ClaudeStreamSession(cwd=cwd, model=model)
    print("→ Turn 1: establish context (no tools needed)")
    t1 = s.send("Remember this token for later: BANANA-7. Reply with just the word: ok")
    print(f"   result: {t1.text!r}")
    print(f"   session_id: {t1.session_id}   events: {sorted(set(t1.event_types))}\n")

    print("→ Turn 2: requires (a) memory of turn 1 and (b) a tool, in the session cwd")
    t2 = s.send(
        "Using the Bash tool, write the token I asked you to remember into a file "
        "named m1_proof.txt in your current working directory. Then reply with the "
        "exact token you wrote."
    )
    print(f"   result: {t2.text!r}")
    print(f"   tool_uses: {t2.tool_uses}   tool_results: {t2.tool_results}")
    print(f"   session_id: {t2.session_id}   usage: {bool(t2.usage)}   cost_usd: {t2.cost_usd}\n")

    s.close()

    proof = pathlib.Path(cwd) / "m1_proof.txt"
    contents = proof.read_text().strip() if proof.exists() else None
    same_session = bool(t1.session_id) and t1.session_id == t2.session_id
    remembered = bool(contents) and "BANANA-7" in contents
    used_tools = bool(t2.tool_uses)
    in_cwd = proof.exists()

    print("===== M1 VERDICT =====")
    print(f"  [{'x' if same_session else ' '}] persistent session (same session_id across 2 turns)")
    print(f"  [{'x' if remembered else ' '}] multi-turn continuity (turn 2 recalled turn 1's token: {contents!r})")
    print(f"  [{'x' if used_tools else ' '}] tool loop inside the session (tool_use: {t2.tool_uses})")
    print(f"  [{'x' if in_cwd else ' '}] per-session cwd (file written under {cwd})")
    ok = same_session and remembered and used_tools and in_cwd
    print(f"\n  M1 {'PASS ✅' if ok else 'INCOMPLETE ❌'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Becoming a Hermes transport (the wiring this PoC defers — kept out of core):
#
#   * New api_mode "claude_stream", opt-in via config (mirrors
#     `model.openai_runtime: codex_app_server`), so it's switchable + low risk.
#   * A session cache keyed by Hermes conversation/session id → one
#     ClaudeStreamSession each (cwd = that conversation's workspace).
#   * Per turn: flatten the new user message(s) → session.send(); map the
#     stream-json events to Hermes' streaming + tool-step rendering (M2) and the
#     `result.usage`/`cost` to Hermes' usage accounting.
#   * Permissions (M3): replace `--permission-mode acceptEdits` with a
#     `--permission-prompt-tool` bridged to Hermes' approval/edit-approval UI
#     (reuse `acp_adapter/edit_approval.py` patterns).
#   * Lifecycle (M5): idle eviction, crash/restart, interrupt/cancel, concurrency.
#
# That wiring is the part that touches Hermes core; everything above does not.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(_demo())
