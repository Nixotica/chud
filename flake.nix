{
  description = "chud — TUI for orchestrating multiple Claude Code agents in parallel";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f {
          inherit system;
          pkgs = import nixpkgs { inherit system; };
        });
    in
    {
      devShells = forAllSystems ({ pkgs, ... }: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.uv
            pkgs.ruff
            pkgs.pyright
            pkgs.git
            self.packages.${pkgs.system}.pre-commit
          ];
        };
      });

      packages = forAllSystems ({ pkgs, ... }: {
        pre-commit = pkgs.writeShellApplication {
          name = "pre-commit";
          runtimeInputs = [ pkgs.python312 pkgs.uv pkgs.ruff pkgs.pyright ];
          text = ''
            set -euo pipefail

            if [ ! -x .venv/bin/pytest ] || [ ! -x .venv/bin/pyright ] || [ requirements.lock -nt .venv/bin/pytest ]; then
              echo ">>> creating .venv and installing pinned deps from requirements.lock"
              uv venv .venv --python 3.12
              uv pip sync --python .venv/bin/python requirements.lock
            fi

            # Always (re)install chud editable — it's the only way pytest can
            # `import chud`, and the previous gating made a failed install
            # sticky across runs.
            uv pip install --python .venv/bin/python -e . --no-deps

            echo ">>> ruff format"
            ruff format .

            echo ">>> ruff check --fix"
            ruff check --fix .

            echo ">>> ruff format --check"
            ruff format --check .

            echo ">>> pyright"
            # shellcheck disable=SC1091
            source .venv/bin/activate
            pyright

            echo ">>> pytest"
            pytest

            echo ">>> all checks passed"
          '';
        };
      });

      formatter = forAllSystems ({ pkgs, ... }: pkgs.nixpkgs-fmt);
    };
}
