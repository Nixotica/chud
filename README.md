# chud

![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)

A terminal UI for orchestrating multiple Claude Code agents in parallel.

Each agent starts in plan mode, surfaces its plan for approval, then continues in the background with auto-accepted edits inside a git worktree. The TUI shows live transcripts and notifies you when an agent needs input or finishes.

## Status

Pre-alpha. Under active development.

## Install

With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv tool install git+https://github.com/Nixotica/chud
```

With pipx:

```sh
pipx install git+https://github.com/Nixotica/chud
```

With pip (in a venv):

```sh
pip install git+https://github.com/Nixotica/chud
```

Then run `chud` from any directory.

## Update

```sh
uv tool upgrade chud   # or: pipx upgrade chud
```

## Develop

```sh
git clone https://github.com/Nixotica/chud
cd chud
pip install -e ".[dev]"
chud           # or: python -m chud
pytest
ruff check .
```

## License

MIT
