from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

KEY_REPO_PATH = "repo_path"
KEY_WORKTREE_PATH = "worktree_path"
KEY_BRANCH = "branch"
KEY_START_HEAD = "start_head"

KEY_ID = "id"
KEY_STATUS = "status"
KEY_INITIAL_PROMPT = "initial_prompt"
KEY_ATTACHED_REPOS = "attached_repos"
KEY_CREATED_AT = "created_at"
KEY_LAST_ACTIVITY_AT = "last_activity_at"
KEY_PENDING_QUESTION = "pending_question"
KEY_ERROR = "error"
KEY_OPTIONS = "options"
KEY_APPROVED_PLAN = "approved_plan"
KEY_EFFORT = "effort"
KEY_ISSUE_NUMBER = "issue_number"


class SessionStatus(StrEnum):
    NEW = "new"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    EXECUTING = "executing"
    AWAITING_USER = "awaiting_user"
    DONE = "done"
    ERRORED = "errored"


@dataclass
class Worktree:
    repo_path: Path
    worktree_path: Path
    branch: str
    start_head: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            KEY_REPO_PATH: str(self.repo_path),
            KEY_WORKTREE_PATH: str(self.worktree_path),
            KEY_BRANCH: self.branch,
            KEY_START_HEAD: self.start_head,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Worktree:
        return cls(
            repo_path=Path(d[KEY_REPO_PATH]),
            worktree_path=Path(d[KEY_WORKTREE_PATH]),
            branch=d[KEY_BRANCH],
            start_head=d.get(KEY_START_HEAD),
        )


@dataclass
class SessionState:
    id: str
    status: SessionStatus = SessionStatus.NEW
    initial_prompt: str = ""
    attached_repos: dict[str, Worktree] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending_question: str | None = None
    error: str | None = None
    options: dict[str, bool] = field(default_factory=dict)
    approved_plan: str | None = None
    effort: str | None = None
    # GitHub issue number this session was launched from, if any. Set when
    # the user picks an issue in the new-session modal; consumed by callers
    # that want to surface "an existing chud is working on issue #N" hints.
    issue_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            KEY_ID: self.id,
            KEY_STATUS: self.status.value,
            KEY_INITIAL_PROMPT: self.initial_prompt,
            KEY_ATTACHED_REPOS: {k: v.to_dict() for k, v in self.attached_repos.items()},
            KEY_CREATED_AT: self.created_at.isoformat(),
            KEY_LAST_ACTIVITY_AT: self.last_activity_at.isoformat(),
            KEY_PENDING_QUESTION: self.pending_question,
            KEY_ERROR: self.error,
            KEY_OPTIONS: dict(self.options),
            KEY_APPROVED_PLAN: self.approved_plan,
            KEY_EFFORT: self.effort,
            KEY_ISSUE_NUMBER: self.issue_number,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionState:
        # Imported lazily to avoid a circular import at module load.
        from chud.options import normalize_effort, normalize_options

        return cls(
            id=d[KEY_ID],
            status=SessionStatus(d[KEY_STATUS]),
            initial_prompt=d.get(KEY_INITIAL_PROMPT, ""),
            attached_repos={
                k: Worktree.from_dict(v) for k, v in d.get(KEY_ATTACHED_REPOS, {}).items()
            },
            created_at=datetime.fromisoformat(d[KEY_CREATED_AT]),
            last_activity_at=datetime.fromisoformat(d[KEY_LAST_ACTIVITY_AT]),
            pending_question=d.get(KEY_PENDING_QUESTION),
            error=d.get(KEY_ERROR),
            options=normalize_options(d.get(KEY_OPTIONS)),
            approved_plan=d.get(KEY_APPROVED_PLAN),
            effort=normalize_effort(d.get(KEY_EFFORT)),
            issue_number=d.get(KEY_ISSUE_NUMBER),
        )


class EventKind(StrEnum):
    STATUS_CHANGED = "status_changed"
    TRANSCRIPT_APPENDED = "transcript_appended"
    PLAN_PROPOSED = "plan_proposed"
    NEEDS_USER_INPUT = "needs_user_input"
    QUESTION_ASKED = "question_asked"
    REPO_ATTACHED = "repo_attached"
    UNKNOWN_MESSAGE = "unknown_message"
    ERROR = "error"
    CLEANUP_REQUESTED = "cleanup_requested"
    PR_REVIEW_REQUESTED = "pr_review_requested"
    PR_PUBLISHED = "pr_published"
    PR_FAILED = "pr_failed"
    WORKTREE_DISCARDED = "worktree_discarded"


@dataclass
class Event:
    session_id: str
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
