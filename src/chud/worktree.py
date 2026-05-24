from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
from git import InvalidGitRepositoryError, NoSuchPathError, Repo
from git.exc import GitCommandError
from gitdb.exc import BadName, BadObject
from githubkit.exception import GitHubException

from chud.gh import _get_client, _resolve_remote_owner_name
from chud.settings import get_branch_prefix, get_include_slug
from chud.state import worktrees_root
from chud.types import SessionState, Worktree

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 40


def _slugify(text: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Make ``text`` safe for a git branch / filesystem path segment.

    Lowercases, replaces runs of non-alphanumerics with ``-``, strips leading
    and trailing ``-``, and truncates at the last ``-`` boundary that fits in
    ``max_len`` (falling back to a hard cut). Returns ``""`` if nothing
    survives normalization (e.g. emoji-only input) — callers decide the
    fallback name in that case.
    """
    if not text:
        return ""
    # Take only the first line; slugs from multi-line prompts get noisy fast.
    first_line = text.splitlines()[0]
    normalized = _SLUG_NON_ALNUM.sub("-", first_line.lower()).strip("-")
    if not normalized:
        return ""
    if len(normalized) <= max_len:
        return normalized
    truncated = normalized[:max_len]
    # Prefer to cut on a hyphen boundary so we don't slice a word in half.
    last_hyphen = truncated.rfind("-")
    if last_hyphen >= max_len // 2:
        truncated = truncated[:last_hyphen]
    return truncated.strip("-") or normalized[:max_len]


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        Repo(path, search_parent_directories=True)
        return True
    except (InvalidGitRepositoryError, NoSuchPathError):
        return False


def repo_toplevel(path: Path) -> Path:
    repo = Repo(path, search_parent_directories=True)
    top = repo.working_tree_dir
    if top is None:
        raise WorktreeError(f"could not resolve toplevel for {path}")
    return Path(top)


def default_base_branch(repo_path: Path) -> str:
    """Resolve the remote's default branch name (e.g. ``main``, ``master``).

    Order of preference:

    1. ``origin/HEAD`` symbolic ref — the canonical answer when the clone
       has been initialized with a remote HEAD.
    2. GitHub REST ``repos.get`` lookup — covers the case where the
       symbolic ref isn't set locally but we have a token + GitHub remote.
    3. ``"main"`` — last-resort fallback so callers always get a string.

    The returned name is *unqualified* (``main``, not ``origin/main``); callers
    decide whether to look it up under ``refs/remotes/origin/`` or
    ``refs/heads/``.
    """
    try:
        repo = Repo(repo_path, search_parent_directories=True)
        head_ref = repo.refs["origin/HEAD"]
        target = head_ref.reference  # type: ignore[attr-defined]
        # ``refs/remotes/origin/main`` → ``main``
        if "/" in target.name:
            return target.name.split("/", 1)[1]
        return target.name
    except (KeyError, IndexError, AttributeError, InvalidGitRepositoryError, NoSuchPathError):
        pass

    client = _get_client()
    if client is not None:
        owner_name = _resolve_remote_owner_name(repo_path)
        if owner_name is not None:
            owner, name = owner_name
            try:
                # The httpx client under the hood is async-only by default;
                # using the sync companion keeps this function sync at the
                # call sites in WorktreeManager. ``GitHub.rest.repos.get``
                # is a regular method that performs a blocking HTTP call.
                resp = client.rest.repos.get(owner, name)
                branch = getattr(resp.parsed_data, "default_branch", None)
                if branch:
                    return str(branch)
            except (GitHubException, httpx.HTTPError) as e:
                log.warning("repos.get default-branch lookup failed for %s/%s: %s", owner, name, e)

    return "main"


def _ref_exists(repo: Repo, ref: str) -> bool:
    try:
        repo.commit(ref)
        return True
    except (ValueError, GitCommandError, BadName, BadObject, KeyError, AttributeError):
        return False


def detect_cwd_repo() -> Path | None:
    """Toplevel of the git repo containing the current working directory.

    Returns ``None`` if CWD is not inside a git repo or if CWD itself
    isn't a real directory anymore. Used by the new-session flow to
    auto-attach the obvious repo without making the user type its path.
    """
    try:
        return repo_toplevel(Path.cwd())
    except (InvalidGitRepositoryError, NoSuchPathError, WorktreeError, OSError):
        return None


class WorktreeManager:
    """Per-session manager for N attached worktrees.

    Worktrees land at ``{worktrees_root}/{session_id}-{basename}``, with a
    ``-2``/``-3``/... suffix when the same basename is attached twice in one
    session (e.g. two repos named ``api``).
    """

    def __init__(self, session: SessionState) -> None:
        self.session = session

    def attach_repo(self, repo_path: Path) -> Worktree:
        """Add a worktree for `repo_path` under the chud worktrees root.

        Idempotent: if the repo is already attached, returns the existing Worktree.
        Disambiguates worktree dir name on basename collision across different repos.
        """
        toplevel = repo_toplevel(repo_path)
        repo_key = str(toplevel)

        if repo_key in self.session.attached_repos:
            return self.session.attached_repos[repo_key]

        root = worktrees_root()
        sid = self.session.id
        basename = toplevel.name
        wt_basename = basename
        i = 2
        existing_dirs = {wt.worktree_path.name for wt in self.session.attached_repos.values()}
        while f"{sid}-{wt_basename}" in existing_dirs:
            wt_basename = f"{basename}-{i}"
            i += 1

        worktree_path = root / f"{sid}-{wt_basename}"
        prefix = get_branch_prefix()
        if get_include_slug():
            slug = _slugify(self.session.initial_prompt) or "session"
            branch = f"{prefix}{slug}-{self.session.id}"
        else:
            branch = f"{prefix}{self.session.id}"

        toplevel_repo = Repo(toplevel)
        branch_exists = _ref_exists(toplevel_repo, branch)

        # Drop stale "prunable" worktree entries (directory gone, .git/worktrees
        # metadata still present) before adding. Without this, a previous chud
        # session whose workspace was rm'd outside `git worktree remove` would
        # keep the branch "checked out" at a missing path and `git worktree
        # add` would fail with "<branch> is already checked out at <path>".
        try:
            toplevel_repo.git.worktree("prune")
        except GitCommandError as e:
            log.debug("git worktree prune failed in %s (continuing): %s", toplevel, e)

        try:
            if branch_exists:
                # Re-attach: never silently rewrite an existing chud branch — that
                # would clobber a previous session's in-progress work. Just check
                # the branch out at its current tip.
                toplevel_repo.git.worktree("add", str(worktree_path), branch)
            else:
                base_ref = self._resolve_clean_base_ref(toplevel)
                toplevel_repo.git.worktree("add", str(worktree_path), "-b", branch, base_ref)
        except GitCommandError as e:
            stderr = (e.stderr or "").strip() or str(e)
            raise WorktreeError(f"git worktree add failed for {toplevel}: {stderr}") from e

        start_head = self._capture_head(worktree_path)
        wt = Worktree(
            repo_path=toplevel,
            worktree_path=worktree_path,
            branch=branch,
            start_head=start_head,
        )
        self.session.attached_repos[repo_key] = wt
        return wt

    @staticmethod
    def _resolve_clean_base_ref(toplevel: Path) -> str:
        """Pick a clean upstream ref to fork a new chud branch from.

        Forking from the parent repo's local ``HEAD`` (``git worktree add -b``
        without a base) silently inherits whatever branch the user — or
        another concurrent chud session — happens to have checked out, which
        is how unrelated WIP commits leak into a fresh session's PR. Instead:

        1. Resolve the remote's default branch (``main``/``master``/...).
        2. Best-effort ``git fetch origin <base>`` — refreshes the remote ref
           so we fork from current upstream tip, not whatever stale ref the
           local clone last saw. Offline/auth failures are logged and the
           fetch is skipped; we fall through to whichever ref exists locally.
        3. Prefer ``origin/<base>``; fall back to local ``<base>`` if the
           remote ref is unavailable; raise ``WorktreeError`` if neither
           exists. A hard error here beats opening a contaminated PR later —
           unusual repos (no main/master at all) are rare enough to deserve
           the explicit failure.
        """
        base = default_base_branch(toplevel)
        repo = Repo(toplevel)

        try:
            repo.remotes.origin.fetch(base)
        except (GitCommandError, AttributeError, ValueError) as e:
            log.warning("git fetch origin %s failed in %s: %s", base, toplevel, e)

        if _ref_exists(repo, f"refs/remotes/origin/{base}"):
            return f"origin/{base}"
        if _ref_exists(repo, f"refs/heads/{base}"):
            return base
        raise WorktreeError(
            f"could not resolve a base ref for new chud branch in {toplevel}: "
            f"no origin/{base} and no local {base}"
        )

    @staticmethod
    def _capture_head(worktree_path: Path) -> str | None:
        try:
            return Repo(worktree_path).head.commit.hexsha
        except (
            ValueError,
            GitCommandError,
            BadName,
            BadObject,
            AttributeError,
            InvalidGitRepositoryError,
        ):
            return None

    def detach_repo(self, repo_key: str, force: bool = False) -> None:
        wt = self.session.attached_repos.get(repo_key)
        if wt is None:
            return

        repo = Repo(wt.repo_path)
        try:
            if force:
                repo.git.worktree("remove", "--force", str(wt.worktree_path))
            else:
                repo.git.worktree("remove", str(wt.worktree_path))
        except GitCommandError as e:
            if not force:
                stderr = (e.stderr or "").strip() or str(e)
                raise WorktreeError(
                    f"git worktree remove failed: {stderr} (pass force=True to discard)"
                ) from e
            # On force, swallow — we're trying our best to clean up.

        del self.session.attached_repos[repo_key]

    def discard_empty_branch(self, repo_key: str) -> None:
        """Drop a chud branch that never received a commit.

        For worktrees where the agent did no work (or where a session was
        cancelled), the ``chud/<slug>-<sid>`` branch ref accumulates in the
        origin repo with no commits behind it. ``cleanup_worktrees`` removes
        the worktree but leaves the branch ref dangling, so call this when
        you know the branch is empty: it force-detaches the worktree (so
        ``git branch -D`` won't complain that it's checked out), then deletes
        the branch ref. Best-effort on the branch deletion — anything other
        than a "not found" failure is logged but swallowed so a stuck branch
        ref doesn't block session cleanup.
        """
        wt = self.session.attached_repos.get(repo_key)
        if wt is None:
            return
        repo_path = wt.repo_path
        branch = wt.branch
        self.detach_repo(repo_key, force=True)
        try:
            Repo(repo_path).git.branch("-D", branch)
        except GitCommandError as e:
            stderr = (e.stderr or "").strip().lower() or str(e).lower()
            if "not found" not in stderr:
                log.warning("git branch -D %s failed in %s: %s", branch, repo_path, stderr)

    def cleanup_worktrees(self) -> None:
        """Remove every attached worktree (force). Destructive."""
        for repo_key in list(self.session.attached_repos):
            self.detach_repo(repo_key, force=True)
