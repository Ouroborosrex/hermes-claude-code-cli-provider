#!/usr/bin/env python3
"""OpenAI-compatible shim for the local Claude Code CLI (`claude -p`).

This is the backend for the ``claude-code-cli`` Hermes provider. It exposes a
minimal OpenAI Chat Completions surface and, for each request, shells out to::

    claude -p --output-format json --model <model> [--effort ..] [--tools ..] ...

then returns the CLI's ``result`` field as the assistant message — the same way
the ``fusion-consult`` skill drives Claude Code as an advisory worker. No
Anthropic API key and no network egress are involved; everything runs through
your local, already-authenticated Claude Code login.

Endpoints
---------
* ``GET  /healthz``               liveness probe
* ``GET  /v1/models``             advertises the model ids the CLI accepts
* ``POST /v1/chat/completions``   chat completion (supports ``stream: true``)

Caveat
------
`claude -p` is a *complete agent*: it runs its own internal tool loop and
returns final text. This shim therefore returns plain assistant text, never
OpenAI-style ``tool_calls``. It is best for chat / advisory / synthesis use.
For Hermes' native tool-calling loop, use the bundled ``anthropic`` provider.

Configuration (environment variables)
-------------------------------------
    CLAUDE_CODE_CLI_HOST            bind host           (default 127.0.0.1)
    CLAUDE_CODE_CLI_PORT            bind port           (default 8765)
    CLAUDE_CODE_CLI_BIN            path to claude       (default: autodetect)
    CLAUDE_CODE_CLI_MODEL          fallback model       (default sonnet)
    CLAUDE_CODE_CLI_EFFORT         reasoning effort     (default high; empty=omit)
    CLAUDE_CODE_CLI_TOOLS          --tools value        (default ""  => no tools)
    CLAUDE_CODE_CLI_DISALLOWED_TOOLS  --disallowedTools (default unset => omit)
    CLAUDE_CODE_CLI_MAX_TURNS      --max-turns          (default 12)
    CLAUDE_CODE_CLI_TIMEOUT        per-request seconds  (default 600)
    CLAUDE_CODE_CLI_EXTRA_ARGS     extra argv, shlex    (default unset)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Model ids the shim advertises and forwards verbatim to `claude --model`.
ADVERTISED_MODELS = ("opus", "sonnet", "haiku")


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None else val


def build_subprocess_env() -> dict[str, str]:
    """Return an env with user-local CLI paths restored.

    Mirrors fusion_runner.build_subprocess_env: cron/gateway-launched parents
    can have a minimal PATH that lacks ~/.local/npm/bin and nvm's node, which
    breaks `claude`'s `/usr/bin/env node` shebang even when the binary resolves.
    """
    env = os.environ.copy()
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = [home / ".local/npm/bin", home / ".local/bin"]

    nvm_versions = home / ".nvm/versions/node"
    if nvm_versions.exists():
        try:
            node_bins = [p for p in nvm_versions.glob("*/bin") if (p / "node").exists()]
            node_bins.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidates.extend(node_bins)
        except OSError:
            pass

    candidates.extend(
        pathlib.Path(p)
        for p in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin")
    )

    parts: list[str] = []
    for p in candidates:
        s = str(p)
        if p.exists() and s not in parts:
            parts.append(s)
    for s in (env.get("PATH") or "").split(os.pathsep):
        if s and s not in parts:
            parts.append(s)
    env["PATH"] = os.pathsep.join(parts)
    return env


def resolve_claude_bin() -> str:
    """Resolve the claude binary: explicit env > PATH > common locations."""
    explicit = _env("CLAUDE_CODE_CLI_BIN").strip()
    if explicit:
        return explicit
    found = shutil.which("claude", path=build_subprocess_env().get("PATH"))
    if found:
        return found
    for cand in ("/usr/bin/claude", "/usr/local/bin/claude",
                 str(pathlib.Path.home() / ".local/npm/bin/claude")):
        if pathlib.Path(cand).exists():
            return cand
    return "claude"  # last resort; will surface a clear "not found" error


# --------------------------------------------------------------------------- #
# Prompt construction + CLI invocation
# --------------------------------------------------------------------------- #
def _content_to_text(content) -> str:
    """Flatten OpenAI message content (str or list-of-parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif part.get("type") == "image_url":
                    chunks.append("[image omitted]")
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(chunks)
    if content is None:
        return ""
    return str(content)


def flatten_messages(messages: list[dict]) -> str:
    """Render an OpenAI message list into a single prompt for `claude -p`.

    System/developer messages become a leading SYSTEM block; the conversation
    is rendered as labelled turns. Tool messages are folded in as context so
    nothing in the history is silently dropped.
    """
    system_blocks: list[str] = []
    convo: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "user").lower()
        text = _content_to_text(msg.get("content"))
        if role in ("system", "developer"):
            if text.strip():
                system_blocks.append(text)
        elif role == "assistant":
            # Surface any tool calls the assistant previously made as context.
            tool_calls = msg.get("tool_calls") or []
            call_desc = ""
            if tool_calls:
                names = [
                    (tc.get("function") or {}).get("name", "tool")
                    for tc in tool_calls if isinstance(tc, dict)
                ]
                call_desc = f" [requested tools: {', '.join(n for n in names if n)}]"
            convo.append(f"ASSISTANT: {text}{call_desc}".rstrip())
        elif role == "tool":
            convo.append(f"TOOL RESULT: {text}")
        else:
            convo.append(f"USER: {text}")

    parts: list[str] = []
    if system_blocks:
        parts.append("SYSTEM INSTRUCTIONS:\n" + "\n\n".join(system_blocks))
    if convo:
        parts.append("CONVERSATION:\n" + "\n\n".join(convo))
    parts.append(
        "Respond as the assistant to the latest user turn. "
        "Return only the response text."
    )
    return "\n\n".join(parts).strip()


def build_claude_argv(model: str) -> list[str]:
    """Assemble the `claude -p` argv from config. Prompt is piped via stdin."""
    argv = [
        resolve_claude_bin(),
        "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
    ]

    effort = _env("CLAUDE_CODE_CLI_EFFORT", "high").strip()
    if effort:
        argv += ["--effort", effort]

    # Default: disable the CLI's own tool loop so it behaves as a text model
    # (Hermes owns tools/sandboxing). Set CLAUDE_CODE_CLI_TOOLS=Read,Bash to allow.
    tools = _env("CLAUDE_CODE_CLI_TOOLS", "")
    argv += ["--tools", tools]

    disallowed = _env("CLAUDE_CODE_CLI_DISALLOWED_TOOLS", "").strip()
    if disallowed:
        argv += ["--disallowedTools", disallowed]

    max_turns = _env("CLAUDE_CODE_CLI_MAX_TURNS", "12").strip()
    if max_turns:
        argv += ["--max-turns", max_turns]

    extra = _env("CLAUDE_CODE_CLI_EXTRA_ARGS", "").strip()
    if extra:
        try:
            argv += shlex.split(extra)
        except ValueError:
            pass
    return argv


def _extract_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def run_claude(prompt: str, model: str) -> dict:
    """Invoke `claude -p` and return {text, usage, error}.

    `usage` is {prompt_tokens, completion_tokens, total_tokens}. On any failure
    `error` is a human-readable string and `text` carries the same message so
    streaming clients still see something.
    """
    argv = build_claude_argv(model)
    try:
        timeout = int(_env("CLAUDE_CODE_CLI_TIMEOUT", "600"))
    except ValueError:
        timeout = 600

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=build_subprocess_env(),
            check=False,
        )
    except FileNotFoundError:
        msg = f"claude binary not found (tried {argv[0]!r}); set CLAUDE_CODE_CLI_BIN"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}
    except subprocess.TimeoutExpired:
        msg = f"claude timed out after {timeout}s"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}
    except Exception as exc:  # defensive: never crash the request thread
        msg = f"claude invocation failed: {exc!r}"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    parsed = _extract_json_object(stdout)

    if parsed is not None:
        if parsed.get("is_error"):
            msg = str(parsed.get("result") or parsed.get("error") or "claude reported is_error")
            return {"text": f"[claude-code-cli error] {msg}", "usage": _map_usage(parsed.get("usage")), "error": msg}
        result = parsed.get("result")
        if not isinstance(result, str):
            result = parsed.get("content")
        if isinstance(result, str) and result.strip():
            return {"text": result.strip(), "usage": _map_usage(parsed.get("usage")), "error": ""}

    # Non-zero exit with no parseable result → surface stderr/stdout tail.
    if proc.returncode != 0:
        tail = (stderr.strip() or stdout.strip())[-1000:]
        msg = f"claude exited {proc.returncode}: {tail}"
        return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}

    # Exit 0 but unparseable → degrade to raw stdout as the completion text.
    fallback = stdout.strip()
    if fallback:
        return {"text": fallback, "usage": _zero_usage(), "error": ""}
    msg = "claude returned no output"
    return {"text": f"[claude-code-cli error] {msg}", "usage": _zero_usage(), "error": msg}


def _zero_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _map_usage(usage) -> dict:
    if not isinstance(usage, dict):
        return _zero_usage()
    prompt = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    completion = int(usage.get("output_tokens", 0) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


# --------------------------------------------------------------------------- #
# OpenAI response shaping
# --------------------------------------------------------------------------- #
def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def build_completion(model: str, text: str, usage: dict) -> dict:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def build_stream_chunks(model: str, text: str, usage: dict, include_usage: bool):
    cid, created = _completion_id(), int(time.time())

    def chunk(delta, finish=None, usage_obj=None):
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage_obj is not None:
            payload["usage"] = usage_obj
        return payload

    yield chunk({"role": "assistant"})
    if text:
        yield chunk({"content": text})
    yield chunk({}, finish="stop")
    if include_usage:
        # OpenAI emits a trailing usage-only chunk with an empty choices list.
        yield {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [], "usage": usage,
        }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "claude-code-cli-shim/1.0"

    # ---- low-level write helpers ----
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str, etype: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": etype}})

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[claude-code-cli] " + (fmt % args) + "\n")

    # ---- routing ----
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/healthz", "/health", "/v1/healthz"):
            self._send_json(200, {"status": "ok", "bin": resolve_claude_bin()})
        elif path == "/v1/models":
            now = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "claude-code-cli"}
                    for m in ADVERTISED_MODELS
                ],
            })
        else:
            self._send_error(404, f"unknown path: {self.path}", "not_found")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/v1/chat/completions":
            self._send_error(404, f"unknown path: {self.path}", "not_found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_error(400, f"invalid JSON body: {exc}")
            return

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_error(400, "'messages' must be a non-empty array")
            return

        model = str(body.get("model") or _env("CLAUDE_CODE_CLI_MODEL", "sonnet")).strip() or "sonnet"
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        prompt = flatten_messages(messages)
        started = time.time()
        outcome = run_claude(prompt, model)
        self.log_message(
            "model=%s stream=%s %.1fs %s", model, stream, time.time() - started,
            "error" if outcome["error"] else "ok",
        )

        if stream:
            self._stream_response(model, outcome, include_usage)
        else:
            self._send_json(200, build_completion(model, outcome["text"], outcome["usage"]))

    def _stream_response(self, model: str, outcome: dict, include_usage: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for chunk in build_stream_chunks(model, outcome["text"], outcome["usage"], include_usage):
                self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-stream


def main() -> int:
    host = _env("CLAUDE_CODE_CLI_HOST", "127.0.0.1")
    try:
        port = int(_env("CLAUDE_CODE_CLI_PORT", "8765"))
    except ValueError:
        port = 8765

    bin_path = resolve_claude_bin()
    if shutil.which(bin_path) is None and not pathlib.Path(bin_path).exists():
        sys.stderr.write(
            f"[claude-code-cli] WARNING: claude binary {bin_path!r} not found on PATH; "
            "requests will fail until CLAUDE_CODE_CLI_BIN points at a valid `claude`.\n"
        )

    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(
        f"[claude-code-cli] serving OpenAI-compatible Claude Code CLI on "
        f"http://{host}:{port}/v1  (claude={bin_path})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[claude-code-cli] shutting down\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
