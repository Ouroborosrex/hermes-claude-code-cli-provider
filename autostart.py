"""Best-effort auto-start of the Claude Code CLI shim.

Imported by ``__init__.py`` at provider-discovery time. If this Hermes is
actually configured to use the ``claude-code-cli`` provider (as the main model
or any auxiliary task) and the shim isn't already listening, spawn it detached
so the provider "just works" without a manual ``start.sh`` — the #1 sharp edge
(a stopped shim after a reboot silently breaks the provider).

Design goals: never raise, never block when the shim is already up, and only
spawn when the provider is genuinely in use.

Controlled by ``CLAUDE_CODE_CLI_AUTOSTART`` (default on; set ``0/false/no/off``
to disable). Closes GitHub issue #1.
"""
from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROVIDER_NAMES = ("claude-code-cli", "claude-cli", "cc-cli", "claude-code-local")


def _disabled() -> bool:
    val = os.environ.get("CLAUDE_CODE_CLI_AUTOSTART", "1").strip().lower()
    return val in {"0", "false", "no", "off"}


def hermes_home() -> pathlib.Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    return pathlib.Path(home) if home else pathlib.Path.home() / ".hermes"


def _host_port() -> tuple[str, int]:
    """Resolve the shim's host/port from the same env the profile honors."""
    host = os.environ.get("CLAUDE_CODE_CLI_HOST", "").strip()
    port = os.environ.get("CLAUDE_CODE_CLI_PORT", "").strip()
    base = os.environ.get("CLAUDE_CODE_CLI_BASE_URL", "").strip()
    if base:
        from urllib.parse import urlparse

        u = urlparse(base)
        host = host or (u.hostname or "")
        port = port or (str(u.port) if u.port else "")
    h = host or DEFAULT_HOST
    if h in ("0.0.0.0", "::", ""):
        h = "127.0.0.1"
    try:
        p = int(port) if port else DEFAULT_PORT
    except ValueError:
        p = DEFAULT_PORT
    return h, p


def _is_up(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _provider_in_use() -> bool:
    """Cheap text scan: is claude-code-cli referenced as a provider in config?

    Erring toward not spawning when config is unreadable keeps unrelated Hermes
    invocations (doctor, --version, sandboxed subprocesses) from launching a
    server they don't need.
    """
    try:
        text = (hermes_home() / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for name in PROVIDER_NAMES:
        for q in ("", "'", '"'):
            if f"provider: {q}{name}{q}" in text:
                return True
    return False


def _acquire_lock(ttl: float = 30.0) -> pathlib.Path | None:
    """Single-flight lock so concurrent importers don't both spawn the shim."""
    lock = pathlib.Path(tempfile.gettempdir()) / "claude-code-cli-autostart.lock"
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) > ttl:
            lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock
    except FileExistsError:
        return None  # someone else is starting it
    except OSError:
        return lock  # lock fs unavailable — proceed without it


def _spawn(server: pathlib.Path) -> None:
    log_dir = hermes_home() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "claude-code-cli-shim.log", "a", buffering=1)
    except OSError:
        log = subprocess.DEVNULL
    env = os.environ.copy()
    env["CLAUDE_CODE_CLI_NO_AUTOSTART"] = "1"  # the child must never re-trigger autostart
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, cwd=str(server.parent)
    )
    if os.name == "posix":
        kwargs["start_new_session"] = True  # detach (setsid) so it outlives this process
    else:  # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    subprocess.Popen([sys.executable, str(server)], **kwargs)


def ensure_server_running(wait_seconds: float = 4.0) -> bool:
    """Start the shim if it's in use and not already listening. Never raises."""
    try:
        if _disabled() or os.environ.get("CLAUDE_CODE_CLI_NO_AUTOSTART"):
            return False
        host, port = _host_port()
        if _is_up(host, port):
            return True
        if not _provider_in_use():
            return False
        server = pathlib.Path(__file__).resolve().parent / "claude_code_server.py"
        if not server.is_file():
            return False

        lock = _acquire_lock()
        if lock is None:
            # another importer is starting it — just wait for it to come up
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if _is_up(host, port):
                    return True
                time.sleep(0.2)
            return _is_up(host, port)

        try:
            _spawn(server)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if _is_up(host, port):
                    return True
                time.sleep(0.2)
            return _is_up(host, port)
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        return False  # autostart is best-effort; never break provider discovery
