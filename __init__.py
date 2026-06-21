"""Claude Code CLI provider profile (local, on-device).

This profile exposes the local **Claude Code CLI** (`claude -p`) to Hermes as
an ordinary OpenAI-compatible provider. It does *not* call the Anthropic API
(the bundled ``anthropic`` profile already does that, and even owns the
``claude-code`` alias). Instead it points at a tiny local shim
(``claude_code_server.py`` in this directory) that translates each
chat-completions request into a `claude -p --output-format json` subprocess
call and returns the CLI's ``result`` field as assistant content — the same way
the ``fusion-consult`` skill drives Claude Code as an advisory worker.

Because it declares ``auth_type="api_key"`` with non-empty ``env_vars``, the
Hermes registry auto-wires it everywhere with no edits to bundled code:

* ``hermes_cli/models.py`` folds it into ``CANONICAL_PROVIDERS`` → it shows up
  in the ``hermes setup`` / ``hermes model`` provider picker.
* ``hermes_cli/auth.py`` folds it into ``PROVIDER_REGISTRY`` → ``hermes model``
  dispatches it through the generic api-key flow.
* The standard ``chat_completions`` transport talks to ``base_url`` over HTTP.

The shim must be running for chat to work — start it with::

    ~/.hermes/plugins/model-providers/claude-code-cli/start.sh

See ``README.md`` in this directory for the full contract, env-var overrides,
and the advisory/text-completion caveat (the CLI runs its own tool loop, so
this provider returns final text, not OpenAI-style ``tool_calls``).
"""

from providers import register_provider
from providers.base import ProviderProfile

# Keep this in sync with the shim's default port (CLAUDE_CODE_CLI_PORT).
# Override at runtime with the CLAUDE_CODE_CLI_BASE_URL env var.
_DEFAULT_SHIM_BASE_URL = "http://127.0.0.1:8765/v1"

claude_code_cli = ProviderProfile(
    name="claude-code-cli",
    # Aliases deliberately avoid "claude-code" — that one belongs to the native
    # `anthropic` profile. These point only at this local-CLI provider.
    aliases=("claude-cli", "cc-cli", "claude-code-local"),
    api_mode="chat_completions",
    display_name="Claude Code (local CLI)",
    description=(
        "Local Claude Code CLI via on-device shim (claude -p) — advisory / "
        "text completions, no API key or network egress"
    ),
    signup_url="",
    # First var = the (ignored) API key the local shim accepts; second var
    # (ends in _BASE_URL) is split out by auth.py as the base-url override.
    env_vars=("CLAUDE_CODE_CLI_API_KEY", "CLAUDE_CODE_CLI_BASE_URL"),
    base_url=_DEFAULT_SHIM_BASE_URL,
    auth_type="api_key",
    supports_vision=False,
    # Model ids the shim accepts and forwards verbatim to `claude --model`.
    # Shown in pickers when the live /models probe is unavailable.
    fallback_models=("opus", "sonnet", "haiku"),
    default_aux_model="haiku",
)

register_provider(claude_code_cli)
