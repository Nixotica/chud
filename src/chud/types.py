from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

KEY_REPO_PATH = "repo_path"
KEY_WORKTREE_PATH = "worktree_path"
KEY_BRANCH = "branch"

KEY_ID = "id"
KEY_WORKSPACE_DIR = "workspace_dir"
KEY_STATUS = "status"
KEY_INITIAL_PROMPT = "initial_prompt"
KEY_ATTACHED_REPOS = "attached_repos"
KEY_CREATED_AT = "created_at"
KEY_LAST_ACTIVITY_AT = "last_activity_at"
KEY_PENDING_QUESTION = "pending_question"
KEY_ERROR = "error"
KEY_OPTIONS = "options"
KEY_APPROVED_PLAN = "approved_plan"


class SessionStatus(str, Enum):
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

    def to_dict(self) -> dict[str, Any]:
        return {
            KEY_REPO_PATH: str(self.repo_path),
            KEY_WORKTREE_PATH: str(self.worktree_path),
            KEY_BRANCH: self.branch,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Worktree:
        return cls(
            repo_path=Path(d[KEY_REPO_PATH]),
            worktree_path=Path(d[KEY_WORKTREE_PATH]),
            branch=d[KEY_BRANCH],
        )


@dataclass
class SessionState:
    id: str
    workspace_dir: Path
    status: SessionStatus = SessionStatus.NEW
    initial_prompt: str = ""
    attached_repos: dict[str, Worktree] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pending_question: str | None = None
    error: str | None = None
    options: dict[str, bool] = field(default_factory=dict)
    approved_plan: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            KEY_ID: self.id,
            KEY_WORKSPACE_DIR: str(self.workspace_dir),
            KEY_STATUS: self.status.value,
            KEY_INITIAL_PROMPT: self.initial_prompt,
            KEY_ATTACHED_REPOS: {k: v.to_dict() for k, v in self.attached_repos.items()},
            KEY_CREATED_AT: self.created_at.isoformat(),
            KEY_LAST_ACTIVITY_AT: self.last_activity_at.isoformat(),
            KEY_PENDING_QUESTION: self.pending_question,
            KEY_ERROR: self.error,
            KEY_OPTIONS: dict(self.options),
            KEY_APPROVED_PLAN: self.approved_plan,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionState:
        # Imported lazily to avoid a circular import at module load.
        from chud.options import normalize_options

        return cls(
            id=d[KEY_ID],
            workspace_dir=Path(d[KEY_WORKSPACE_DIR]),
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
        )


class EventKind(str, Enum):
    STATUS_CHANGED = "status_changed"
    TRANSCRIPT_APPENDED = "transcript_appended"
    PLAN_PROPOSED = "plan_proposed"
    NEEDS_USER_INPUT = "needs_user_input"
    QUESTION_ASKED = "question_asked"
    REPO_ATTACHED = "repo_attached"
    UNKNOWN_MESSAGE = "unknown_message"
    ERROR = "error"
    CLEANUP_REQUESTED = "cleanup_requested"
    PR_PUBLISHED = "pr_published"
    PR_FAILED = "pr_failed"


@dataclass
class Event:
    session_id: str
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
