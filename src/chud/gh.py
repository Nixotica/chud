"""Thin wrappers around the ``gh`` CLI plus a generic async subprocess runner.

Two responsibilities live here:

1. **GitHub-issue helpers** for the new-session modal: ``Issue``,
   ``is_available``, ``list_issues``, and the pure ``build_issue_prompt``
   formatter. These let the TUI offer "link this session to issue #X" without
   blocking the modal on a slow network — every failure mode collapses to an
   empty list and the picker hides silently.
2. **Shared subprocess plumbing** (``_run``, ``_no_prompt_env``,
   ``_RUN_TIMEOUT_S``) used by both the issue lookups here and the PR-publish
   flow in ``chud.pr``. Centralizing them avoids two copies of "kill on
   timeout, hide credential prompts, never block the TUI" logic drifting out
   of sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# A real GitHub push completes in a few seconds; anything past this is a
# hang, typically a credential prompt that the TUI's raw-mode terminal
# can't satisfy. Without this guard, ``pr.publish_draft_prs`` parks
# forever and the post-DONE CLEANUP_REQUESTED broadcast never fires.
_RUN_TIMEOUT_S = 60.0

# Tight ceiling for the new-session-modal-open path so a slow network or
# unauthenticated ``gh`` never delays the modal opening. On timeout the
# call returns an empty list and the picker is hidden silently.
_LIST_ISSUES_TIMEOUT_S = 5.0

# Cap on the issue body length we splice into the agent prompt. Long
# issue bodies can balloon prompt size and push past the model's context
# window for no real benefit; the agent can always re-read the URL if it
# needs the full text.
_BODY_TRUNCATION_LIMIT = 8000
_BODY_TRUNCATION_MARKER = "\n\n…(truncated)…"


def _no_prompt_env() -> dict[str, str]:
    """Env that forbids git/ssh from waiting on an interactive credential prompt.

    Without these, ``git push`` inheriting the TUI's TTY can either block on
    stdin (raw mode swallows the keypresses git expects) or scribble a
    password prompt onto the screen — both manifest as "TUI got laggy and
    nothing happened." With them set, git/ssh fail fast with an auth error
    that surfaces as a PR_FAILED toast.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "/bin/true")
    env.setdefault("SSH_ASKPASS", "/bin/true")
    env.setdefault("SSH_ASKPASS_REQUIRE", "never")
    return env


async def _run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    timeout: float = _RUN_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run a subprocess, returning (returncode, stdout, stderr).

    stdin is wired to /dev/null so git/gh can never read from the parent
    TTY, and a wall-clock ``timeout`` kills any process that still hangs
    despite that — preserving the invariant that callers always reach
    their post-await cleanup paths.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_no_prompt_env(),
    )
    try:
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            log.warning("gh._run timeout after %.0fs: %s", timeout, cmd)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return (-1, "", f"timed out after {timeout:.0f}s")
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode(errors="replace").strip(),
            err_b.decode(errors="replace").strip(),
        )
    finally:
        # asyncio.subprocess.Process.communicate()/wait() don't close the
        # underlying BaseSubprocessTransport — that only happens when the
        # Process is GC'd, which may be after our event loop has shut down.
        # When that race loses we get "RuntimeError: Event loop is closed"
        # tracebacks from BaseSubprocessTransport.__del__ -> close() trying
        # to schedule connection_lost on a dead loop. Closing the transport
        # explicitly here resolves it deterministically while the loop is
        # still alive. (The attribute is private but stable across CPython.)
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


@dataclass(frozen=True)
class Issue:
    """A GitHub issue surfaced for the new-session picker.

    Frozen because instances cross modal boundaries and end up baked into
    ``NewSessionResult``; immutability rules out a stale-reference bug if
    the underlying list is ever mutated.
    """

    number: int
    title: str
    url: str
    body: str
    state: str  # "OPEN" | "CLOSED" — only OPEN is fetched today, kept for clarity.


def is_available() -> bool:
    """Return True iff the ``gh`` CLI is on PATH.

    Cheap pre-flight so the new-session flow can skip spawning subprocesses
    on machines without GitHub CLI installed.
    """
    return shutil.which("gh") is not None


async def list_issues(
    repo_path: Path,
    *,
    limit: int = 20,
    timeout: float = _LIST_ISSUES_TIMEOUT_S,
) -> list[Issue]:
    """Best-effort list of recent open issues for the repo at ``repo_path``.

    Calls ``gh issue list --state open --limit N --json number,title,url,body,state``
    with ``cwd=repo_path`` so ``gh`` infers the GitHub repo from the local
    remote. Normalizes CRLF → LF in the body so downstream prompt formatting
    stays clean.

    Every failure mode — non-zero exit (no auth, no remote, not GitHub-
    hosted), timeout, malformed JSON, missing keys — collapses to an empty
    list and a WARNING log line. The caller treats empty-list as "no
    picker," which is exactly what we want for the silent-degrade UX.
    """
    rc, out, err = await _run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,body,state",
        ],
        cwd=repo_path,
        timeout=timeout,
    )
    if rc != 0:
        log.warning("gh issue list failed (rc=%d) in %s: %s", rc, repo_path, err)
        return []
    if not out:
        return []
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        log.warning("gh issue list returned invalid JSON in %s: %s", repo_path, e)
        return []
    if not isinstance(raw, list):
        log.warning("gh issue list JSON is not a list in %s: %r", repo_path, type(raw))
        return []
    issues: list[Issue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            issues.append(
                Issue(
                    number=int(item["number"]),
                    title=str(item["title"]),
                    url=str(item["url"]),
                    body=str(item.get("body", "") or "").replace("\r\n", "\n"),
                    state=str(item.get("state", "OPEN")),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning("gh issue list item skipped (%s): %r", e, item)
            continue
    return issues


_ISSUES_WITH_OPEN_PR_QUERY = """\
query($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, states: OPEN) {
      nodes {
        closingIssuesReferences(first: 20) {
          nodes { number }
        }
      }
    }
  }
}
"""


async def list_issue_numbers_with_open_pr(
    repo_path: Path,
    *,
    pr_limit: int = 100,
    timeout: float = _LIST_ISSUES_TIMEOUT_S,
) -> set[int]:
    """Return the set of issue numbers that have any open PR linked to them.

    Uses ``gh api graphql`` to read each open PR's ``closingIssuesReferences``
    — which covers both closing-keyword references in the PR body / commits
    (``closes #N``) and links added manually through GitHub's Development
    sidebar. The new-session picker uses the result to hide issues already
    in review, regardless of who or what made the PR.

    The ``--json`` flag on ``gh issue list`` doesn't expose this field on
    older gh versions (we hit that on 2.x in the field), so we go straight
    to GraphQL — which always has it.

    Every failure mode (no auth, owner/name lookup fails, timeout,
    malformed JSON) collapses to an empty set; the caller treats that as
    "no filter info" and the picker stays unfiltered rather than empty.
    """
    rc, out, err = await _run(
        ["gh", "repo", "view", "--json", "owner,name"],
        cwd=repo_path,
        timeout=timeout,
    )
    if rc != 0:
        log.warning("gh repo view failed (rc=%d) in %s: %s", rc, repo_path, err)
        return set()
    try:
        meta = json.loads(out)
        owner = str(meta["owner"]["login"])
        name = str(meta["name"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("gh repo view returned unexpected JSON in %s: %s", repo_path, e)
        return set()

    rc, out, err = await _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_ISSUES_WITH_OPEN_PR_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"first={pr_limit}",
        ],
        cwd=repo_path,
        timeout=timeout,
    )
    if rc != 0:
        log.warning("gh api graphql (PRs) failed (rc=%d) in %s: %s", rc, repo_path, err)
        return set()
    try:
        raw = json.loads(out)
        nodes = raw["data"]["repository"]["pullRequests"]["nodes"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("gh api graphql (PRs) returned unexpected JSON in %s: %s", repo_path, e)
        return set()
    if not isinstance(nodes, list):
        return set()

    out_set: set[int] = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        closing = n.get("closingIssuesReferences") or {}
        pr_nodes = closing.get("nodes") if isinstance(closing, dict) else None
        if not isinstance(pr_nodes, list):
            continue
        for ref in pr_nodes:
            if not isinstance(ref, dict):
                continue
            try:
                out_set.add(int(ref["number"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out_set


def build_issue_prompt(issue: Issue, user_prompt: str) -> str:
    """Render the agent's initial prompt with a linked GitHub issue prepended.

    Layout::

        GitHub issue #{number}: {title}
        {url}

        {body}

        ---

        {user_prompt}

    The body is truncated at ``_BODY_TRUNCATION_LIMIT`` characters with an
    explicit marker so the agent can tell content was cut. If the body is
    blank/whitespace-only the body block is omitted entirely (no dangling
    empty section before the ``---`` separator).
    """
    body = issue.body.strip()
    if len(body) > _BODY_TRUNCATION_LIMIT:
        body = body[:_BODY_TRUNCATION_LIMIT] + _BODY_TRUNCATION_MARKER

    head = f"GitHub issue #{issue.number}: {issue.title}\n{issue.url}"
    user_part = user_prompt.strip()
    if body:
        return f"{head}\n\n{body}\n\n---\n\n{user_part}"
    return f"{head}\n\n---\n\n{user_part}"
