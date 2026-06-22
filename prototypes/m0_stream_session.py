#!/usr/bin/env python3
"""M0 spike (issue #7): drive Claude Code via stream-json and observe its events.

Proves the primitive behind a Codex-parity integration: running `claude` with
`--output-format stream-json --verbose` lets a host observe the agent's full
event stream — system/init, assistant text, `tool_use`, tool results, and a
final `result` carrying usage + cost — on the Claude Code subscription (no API
key). Multi-turn input via `--input-format stream-json` is M1; this M0 driver
uses a single positional prompt to validate the OUTPUT taxonomy + billing fields
that M2 (event mapping) would consume.

Usage:   python3 m0_stream_session.py ["task prompt"]
Env:     M0_MODEL (default sonnet), CLAUDE_CODE_CLI_BIN (default: autodetect),
         M0_TOOLS (default "Bash,Read,Glob,Grep"), M0_TIMEOUT (default 180)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else (
        "Use the Bash tool to run `echo hello-from-stream-json`, "
        "then report the exact output in one sentence."
    )
    model = os.environ.get("M0_MODEL", "sonnet")
    bin_ = os.environ.get("CLAUDE_CODE_CLI_BIN") or shutil.which("claude") or "/usr/bin/claude"
    tools = os.environ.get("M0_TOOLS", "Bash,Read,Glob,Grep")
    try:
        timeout = int(os.environ.get("M0_TIMEOUT", "180"))
    except ValueError:
        timeout = 180

    argv = [
        bin_, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages",
        "--model", model,
        "--permission-mode", "acceptEdits",
        "--allowedTools", tools,
        "--no-session-persistence",
    ]
    sys.stderr.write(
        f"$ claude -p <prompt> --output-format stream-json --verbose "
        f"--model {model} --allowedTools {tools}\n\n"
    )

    started = time.time()
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    event_counts: dict[str, int] = {}
    tool_uses: list[str] = []
    tool_results = 0
    partials = 0
    result_ev: dict | None = None
    deadline = started + timeout

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "?")
            sub = ev.get("subtype")
            event_counts[f"{t}/{sub}" if sub else t] = (
                event_counts.get(f"{t}/{sub}" if sub else t, 0) + 1
            )
            if t == "assistant":
                for b in ((ev.get("message") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_uses.append(b.get("name", "?"))
            elif t == "user":
                for b in ((ev.get("message") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tool_results += 1
            elif t == "stream_event":
                partials += 1
            elif t == "result":
                result_ev = ev
                break
            if time.time() > deadline:
                proc.kill()
                break
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:
            pass

    stderr_tail = (proc.stderr.read() if proc.stderr else "") or ""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    print("\n===== M0 stream-json result =====")
    print(f"duration: {time.time() - started:.1f}s   exit: {proc.returncode}")
    print("event types seen:")
    for k, v in sorted(event_counts.items()):
        print(f"  {k}: {v}")
    print(f"tool_use blocks: {tool_uses or '(none)'}")
    print(f"tool_result blocks: {tool_results}")
    print(f"partial (stream_event) chunks: {partials}")
    if result_ev:
        res = result_ev.get("result")
        print("\nresult event:")
        print(f"  is_error:       {result_ev.get('is_error')}")
        print(f"  result text:    {str(res)[:200]!r}")
        print(f"  usage:          {result_ev.get('usage')}")
        print(f"  total_cost_usd: {result_ev.get('total_cost_usd')}")
        print(f"  num_turns:      {result_ev.get('num_turns')}")
        print(f"  duration_ms:    {result_ev.get('duration_ms')}")
        print(f"  session_id:     {result_ev.get('session_id')}")
    else:
        print("\n(no result event captured)")
    if stderr_tail and not result_ev:
        print("\nstderr tail:\n" + stderr_tail[-1500:])

    ok = bool(result_ev) and not result_ev.get("is_error")
    print("\nVERDICT (M0 acceptance):")
    print(f"  [{'x' if ok else ' '}] final result event received")
    print(f"  [{'x' if tool_uses else ' '}] tool_use events observed (the tool loop is visible to the host)")
    print(f"  [{'x' if result_ev and result_ev.get('usage') is not None else ' '}] usage present")
    print(f"  [{'x' if result_ev and result_ev.get('total_cost_usd') is not None else ' '}] cost present")
    print("\nNote: invoked with no API key → authenticated via the Claude Code "
          "subscription, same as `claude -p`. M1 adds --input-format stream-json "
          "for multi-turn + per-session cwd.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
