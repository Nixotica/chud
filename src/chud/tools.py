"""In-process MCP tools exposed to the agent.

Currently we only ship one tool — ``attach_repo`` — which lets the agent
add a git repo as a session worktree mid-run. This is the escape hatch for
the "user launched chud above several repos" case: the modal no longer
asks for a repo path, so when CWD isn't a git repo the session starts
unattached and the agent uses ``ls``/``AskUserQuestion`` and this tool to
attach what it needs.

The MCP server is constructed per ``AgentSession`` so the tool callback
is bound to that session's manager (via the ``attach`` callable) and can't
leak attaches across sessions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

# Async callback the tool uses to attach a repo to the calling session.
# Implemented by SessionManager (bound per-session in create_session()).
AttachCallback = Callable[[Path], Awaitable[None]]


def build_chud_mcp_server(attach: AttachCallback) -> Any:
    """Build the per-session ``chud`` MCP server.

    Returns the SDK server config dict that goes into
    ``ClaudeAgentOptions.mcp_servers["chud"]``. The agent then sees the
    tool as ``mcp__chud__attach_repo``.
    """

    @tool(
        "attach_repo",
        (
            "Attach a local git repo to this chud session as a worktree. "
            "Use when you discover a repo you need to work on (e.g. when "
            "the session was launched from a parent directory containing "
            "several repos). Prefer to confirm with the user via "
            "AskUserQuestion first when more than one candidate looks "
            "plausible. The path must be the absolute path to a git repo "
            "toplevel."
        ),
        {"path": str},
    )
    async def attach_repo(args: dict[str, Any]) -> dict[str, Any]:
        repo = Path(args["path"]).expanduser().resolve()
        try:
            await attach(repo)
        except Exception as e:
            return {
                "content": [
                    {"type": "text", "text": f"Failed to attach {repo}: {e!r}"}
                ],
                "is_error": True,
            }
        return {
            "content": [
                {"type": "text", "text": f"Attached {repo} as a worktree."}
            ]
        }

    return create_sdk_mcp_server("chud", tools=[attach_repo])
