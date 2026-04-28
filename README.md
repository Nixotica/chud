# chud

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
```

### With Nix

A [flake](./flake.nix) provides a dev shell with Python 3.12, `uv`, `ruff`, and `pyright` pinned. CI uses the same shell.

```sh
nix develop                       # enter the shell
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/chud
```

### Quality gates

Run before pushing — CI runs the same (under `nix develop`):

```sh
pytest                    # tests
ruff check .              # lint
ruff format .             # auto-format (CI runs `ruff format --check .`)
pyright                   # static type-check (same engine as VS Code Pylance)
```

Or run them all in one shot via the flake (auto-formats first, then lint + format check + pyright + pytest):

```sh
nix run .#pre-commit
```

The same script is on `PATH` as `pre-commit` inside `nix develop`.

## License

MIT
