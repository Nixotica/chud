"""GitHub helpers backed by ``githubkit`` (REST + GraphQL via httpx).

Two responsibilities live here:

1. **GitHub-issue helpers** for the new-session modal: ``Issue``,
   ``is_available``, ``list_issues``, and the pure ``build_issue_prompt``
   formatter. These let the TUI offer "link this session to issue #X" without
   blocking the modal on a slow network — every failure mode collapses to an
   empty list and the picker hides silently.
2. **Shared GitHub-client plumbing** (``_get_client``,
   ``_resolve_remote_owner_name``) used by both the issue lookups here and
   the PR-publish flow in ``chud.pr``. Centralizing them avoids two clients
   competing for auth / connection pools.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import httpx
from git import InvalidGitRepositoryError, NoSuchPathError, Repo
from githubkit import GitHub
from githubkit.exception import GitHubException
from githubkit.utils import UNSET

from chud._auth import resolve_github_token

log = logging.getLogger(__name__)

# Tight ceiling for the new-session-modal-open path so a slow network or
# unauthenticated token never delays the modal opening. On timeout the
# call returns an empty list and the picker is hidden silently.
_LIST_ISSUES_TIMEOUT_S = 5.0

# A real GitHub call completes in a few seconds; anything past this is a
# hang, typically a flaky connection. Without this guard, ``pr.publish_draft_prs``
# could park forever and the post-DONE CLEANUP_REQUESTED broadcast would never fire.
_REQUEST_TIMEOUT_S = 60.0

# Cap on the issue body length we splice into the agent prompt. Long
# issue bodies can balloon prompt size and push past the model's context
# window for no real benefit; the agent can always re-read the URL if it
# needs the full text.
_BODY_TRUNCATION_LIMIT = 8000
_BODY_TRUNCATION_MARKER = "\n\n…(truncated)…"

_GITHUB_REMOTE_RE = re.compile(
    r"""^
    (?:
        git@github\.com:                    # SSH form: git@github.com:owner/name
        |https?://(?:[^@/]+@)?github\.com/  # HTTPS form, optional userinfo
        |ssh://git@github\.com/             # Explicit ssh:// form
    )
    (?P<owner>[^/]+)/(?P<name>[^/]+?)       # owner/name
    (?:\.git)?$                             # optional trailing .git
    """,
    re.VERBOSE,
)


@cache
def _get_client() -> GitHub | None:
    """Return a memoized authenticated ``GitHub`` client, or ``None`` if no
    token is available.

    This is the seam tests monkeypatch — replacing it with a client wired
    to ``respx.MockRouter`` keeps them off the real network.
    """
    token = resolve_github_token()
    if token is None:
        return None
    timeout = httpx.Timeout(_REQUEST_TIMEOUT_S)
    return GitHub(token, timeout=timeout)


def _resolve_remote_owner_name(repo_path: Path) -> tuple[str, str] | None:
    """Extract ``(owner, name)`` from the ``origin`` remote of ``repo_path``.

    Handles the common GitHub remote URL shapes:

    - ``git@github.com:owner/name.git``
    - ``https://github.com/owner/name`` (with or without ``.git``)
    - ``ssh://git@github.com/owner/name.git``

    Returns ``None`` when the path isn't a git repo, has no ``origin``, or
    points at a non-GitHub remote — caller treats those the same as
    "no GitHub features for this repo."
    """
    try:
        repo = Repo(repo_path, search_parent_directories=True)
        origin = repo.remotes.origin
        urls = list(origin.urls)
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError, AttributeError):
        return None
    for url in urls:
        m = _GITHUB_REMOTE_RE.match(url.strip())
        if m:
            return m.group("owner"), m.group("name")
    return None


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
    """Return True iff a GitHub token is configured (env var or ``hosts.yml``).

    Cheap pre-flight so the new-session flow can skip GitHub calls on
    machines without auth set up.
    """
    return _get_client() is not None


async def list_issues(
    repo_path: Path,
    *,
    limit: int = 20,
    timeout: float = _LIST_ISSUES_TIMEOUT_S,
) -> list[Issue]:
    """Best-effort list of recent open issues for the repo at ``repo_path``.

    Resolves ``origin`` to ``(owner, name)`` via the local git remote, then
    fetches via the GitHub REST API. Normalizes CRLF → LF in the body so
    downstream prompt formatting stays clean.

    Every failure mode — no auth, no remote, non-GitHub remote, network
    error, malformed payload — collapses to an empty list and a WARNING
    log line. The caller treats empty-list as "no picker," which is
    exactly what we want for the silent-degrade UX.
    """
    client = _get_client()
    if client is None:
        return []
    owner_name = _resolve_remote_owner_name(repo_path)
    if owner_name is None:
        return []
    owner, name = owner_name

    try:
        resp = await asyncio.wait_for(
            client.rest.issues.async_list_for_repo(
                owner,
                name,
                state="open",
                per_page=limit,
            ),
            timeout=timeout,
        )
    except (GitHubException, httpx.HTTPError, TimeoutError) as e:
        log.warning("list_issues failed in %s: %s", repo_path, e)
        return []

    issues: list[Issue] = []
    for item in resp.parsed_data:
        # GitHub's issues endpoint returns pull requests too; skip them so
        # the picker doesn't surface PRs as if they were issues. githubkit
        # marks the field as ``UNSET`` (its missing-field sentinel) on real
        # issues and populates a struct on PRs, so both ``None`` and
        # ``UNSET`` mean "this is an issue."
        pr_field = getattr(item, "pull_request", None)
        if pr_field is not None and pr_field is not UNSET:
            continue
        try:
            body_raw = getattr(item, "body", None) or ""
            issues.append(
                Issue(
                    number=int(item.number),
                    title=str(item.title),
                    url=str(item.html_url),
                    body=str(body_raw).replace("\r\n", "\n"),
                    state=str(item.state).upper(),
                )
            )
        except (AttributeError, TypeError, ValueError) as e:
            log.warning("list_issues item skipped (%s)", e)
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

    Reads each open PR's ``closingIssuesReferences`` via GraphQL — which
    covers both closing-keyword references in the PR body / commits
    (``closes #N``) and links added manually through GitHub's Development
    sidebar. The new-session picker uses the result to hide issues
    already in review, regardless of who or what made the PR.

    Every failure mode (no auth, owner/name lookup fails, network error,
    malformed payload) collapses to an empty set; the caller treats that
    as "no filter info" and the picker stays unfiltered rather than empty.
    """
    client = _get_client()
    if client is None:
        return set()
    owner_name = _resolve_remote_owner_name(repo_path)
    if owner_name is None:
        return set()
    owner, name = owner_name

    try:
        data = await asyncio.wait_for(
            client.async_graphql(
                _ISSUES_WITH_OPEN_PR_QUERY,
                variables={"owner": owner, "name": name, "first": pr_limit},
            ),
            timeout=timeout,
        )
    except (GitHubException, httpx.HTTPError, TimeoutError) as e:
        log.warning("list_issue_numbers_with_open_pr failed in %s: %s", repo_path, e)
        return set()

    try:
        nodes = data["repository"]["pullRequests"]["nodes"]
    except (KeyError, TypeError) as e:
        log.warning("list_issue_numbers_with_open_pr unexpected JSON in %s: %s", repo_path, e)
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
