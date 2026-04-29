# chud

A Textual-based TUI for orchestrating multiple Claude Code agents in parallel. Each session wraps a `ClaudeSDKClient` (from `claude-agent-sdk`), runs in its own per-repo git worktree, starts in plan mode, and only auto-accepts edits after the user approves the proposed plan. Sessions and worktree metadata persist under `~/.local/share/chud/`.

Status: pre-alpha — breaking changes expected.

## Common commands

Run from the repo root:

| Task | Command |
| --- | --- |
| Install (editable + dev deps) | `pip install -e ".[dev]"` |
| Launch the TUI | `chud` *or* `python -m chud` |
| Run all tests | `pytest` |
| Run one test | `pytest tests/test_worktree.py::test_name` |
| Lint | `ruff check .` |
| Format check | `ruff format --check .` |
| Type check | `pyright` |

- Entry point: `chud.app:main` (defined in `pyproject.toml` under `[project.scripts]`).
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (see `[tool.pytest.ini_options]`).
- Runtime log: `~/.local/share/chud/chud.log`.

### PR checks

The PR workflow (`.github/workflows/tests.yml`) runs `pytest`, `ruff check .`, `ruff format --check .`, and `pyright` — all four must pass. `pyright` runs in `basic` mode against `src` and `tests` (configured under `[tool.pyright]` in `pyproject.toml`). Fix type errors at the source; do not silence them with `# type: ignore` unless there's a real reason (e.g. a deliberate runtime monkey-patch like the SDK method assignments in `tests/test_prompt_queue.py`).

## Architecture

All source lives under `src/chud/`. The module split is deliberate — keep concerns where they are:

- `app.py` — `ChudApp`, the Textual app. Top-level layout, key bindings, modal orchestration. Bindings: `n` new session, `k` kill session, `q` quit. `main()` configures logging and runs the app.
- `manager.py` — `SessionManager`. Owns all `AgentSession` instances, fans events from sessions to UI subscribers via async queues, triggers desktop notifications when the app is unfocused, and persists session state.
- `session.py` — `AgentSession`. Wraps one `ClaudeSDKClient` and runs the per-session state machine. Intercepts the `ExitPlanMode` tool call to gate plan approval; uses `Stop` and `Notification` SDK hooks to detect idle and user-input requests.
- `state.py` — JSON persistence. Resolves data dirs via `platformdirs.user_data_dir("chud")`. Atomic writes (temp file + rename).
- `types.py` — Data models: `SessionStatus` enum, `SessionState`, `Worktree`, `Event`, `EventKind`. All dataclasses; JSON round-trippable.
- `worktree.py` — `WorktreeManager`. Creates one git worktree per attached repo on branch `chud/{session_id}`. Idempotent re-attach. Disambiguates basename collisions (e.g., two repos named `api` → `api`, `api-2`). Raises `WorktreeError`.
- `notify.py` — Thin wrapper around `notify-send` for desktop notifications.
- `widgets/` — Pure Textual UI components:
  - `session_list.py` — left pane, list of all sessions with status glyph + prompt preview.
  - `session_view.py` — right pane: header, transcript, input box. `render_event()` dispatches by `EventKind`.
  - `plan_modal.py` — modal showing the proposed plan markdown; `a` approve, `r`/`escape` reject.
  - `new_session_modal.py` — repo path (optional) + initial prompt (required).

### Session state machine

Defined by `SessionStatus` in `src/chud/types.py`. Anything in `session.py` that mutates status must respect this:

```
NEW → PLANNING → AWAITING_PLAN_APPROVAL → EXECUTING ↔ AWAITING_USER → DONE | ERRORED
```

`EXECUTING` and `AWAITING_USER` ping-pong while the agent works. Terminal states are `DONE` and `ERRORED`.

### Event kinds

`EventKind` (`src/chud/types.py`) — what `AgentSession` emits onto its async queue:
`STATUS_CHANGED`, `TRANSCRIPT_APPENDED`, `PLAN_PROPOSED`, `NEEDS_USER_INPUT`, `REPO_ATTACHED`, `UNKNOWN_MESSAGE`, `ERROR`.

## Persistence layout

All under `~/.local/share/chud/` (resolved via `platformdirs`):

- `sessions.json` — all session metadata (one document, atomic write).
- `workspaces/{session_id}/` — per-session worktrees, one subdir per attached repo.
- `chud.log` — application log.

`.gitignore` excludes `workspaces/` and `.chud-local/` so runtime data never gets committed.

## Conventions

- Python `>=3.10`. Full type hints throughout, including async signatures.
- Async-first: `asyncio.Queue`, `asyncio.Task`, `async def` for any I/O.
- `snake_case` for functions/vars, `PascalCase` for classes, module + class docstrings.
- Custom exceptions for failure boundaries (e.g. `WorktreeError`).
- `contextlib.suppress` for best-effort cleanup paths.
- Lint/format via `ruff` — line length `100`, target `py310`, rules `E,F,I,UP,B,SIM` (`pyproject.toml`).

## Dependencies

Runtime (`pyproject.toml`):

- `claude-agent-sdk>=0.1.0` — agent runtime.
- `textual>=8.2.4` — TUI framework.
- `platformdirs>=4.0` — cross-platform data dirs.

Dev: `pytest>=9.0.3`, `pytest-asyncio>=1.3.0`, `ruff>=0.5`. Build backend: `hatchling`.

## Gotchas

- **Plan mode is the safety gate.** Sessions auto-accept edits *after* plan approval. If you change `session.py`, do not bypass the `ExitPlanMode` interception or the `AWAITING_PLAN_APPROVAL` state — that's the only thing standing between an agent and unreviewed edits.
- **Worktree re-attach is idempotent.** `WorktreeManager` reuses an existing worktree on the `chud/{session_id}` branch rather than failing — relevant when re-attaching after a crash.
- **Persistence is local-only.** There's no remote sync. The `workspaces/` dir is intentionally `.gitignore`'d.
- **Notifications need `notify-send`.** `notify.py` is a thin wrapper; missing binary degrades silently.
- Pre-alpha: APIs and on-disk formats may change without migration support.
