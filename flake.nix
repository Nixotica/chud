{
  description = "chud — TUI for orchestrating multiple Claude Code agents in parallel";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems }:
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
          lib = nixpkgs.lib;
        });

      # uv2nix workspace + dependency overlay are system-independent.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      uvOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      # Local override: hatch-vcs has no .git inside the Nix sandbox, so we
      # have to feed it a version via the env var setuptools-scm honours.
      localOverlay = final: prev: {
        chud = prev.chud.overrideAttrs (old: {
          env = (old.env or { }) // {
            SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CHUD =
              if self ? shortRev then self.shortRev else "0.0.0+dirty";
          };
        });
      };

      mkChud = { pkgs, lib, ... }:
        let
          pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
            python = pkgs.python312;
          }).overrideScope (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            uvOverlay
            localOverlay
          ]);
          chudVenv = pythonSet.mkVirtualEnv "chud-env" workspace.deps.default;
        in
        pkgs.runCommand "chud"
          {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            meta = {
              description = "TUI for orchestrating multiple Claude Code agents in parallel";
              homepage = "https://github.com/Nixotica/chud";
              license = lib.licenses.mit;
              mainProgram = "chud";
            };
          } ''
          mkdir -p $out/bin
          makeWrapper ${chudVenv}/bin/chud $out/bin/chud \
            --prefix PATH : ${lib.makeBinPath [ pkgs.git pkgs.gh pkgs.libnotify ]}
        '';
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
            pkgs.gh
            self.packages.${pkgs.system}.pre-commit
            self.packages.${pkgs.system}.release
          ];
        };
      });

      packages = forAllSystems ({ pkgs, lib, system }: {
        default = mkChud { inherit pkgs lib; };

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

        release = pkgs.writeShellApplication {
          name = "release";
          runtimeInputs = [ pkgs.git pkgs.gh ];
          text = ''
            set -euo pipefail

            if [ $# -ne 1 ]; then
              echo "usage: release X.Y.Z" >&2
              exit 2
            fi

            version="$1"
            if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-z0-9.-]+)?$ ]]; then
              echo "error: version must look like X.Y.Z (got '$version')" >&2
              exit 2
            fi

            tag="v$version"
            branch="release/$tag"

            current_branch=$(git rev-parse --abbrev-ref HEAD)
            if [ "$current_branch" != "main" ]; then
              echo "error: must be on main (currently on '$current_branch')" >&2
              exit 2
            fi

            if ! git diff --quiet || ! git diff --cached --quiet; then
              echo "error: working tree has uncommitted changes" >&2
              exit 2
            fi

            echo ">>> fetching origin"
            git fetch --tags origin main

            if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
              echo "error: local main is not in sync with origin/main" >&2
              exit 2
            fi

            if git rev-parse "$tag" >/dev/null 2>&1; then
              echo "error: tag $tag already exists locally" >&2
              exit 2
            fi
            if git ls-remote --tags --exit-code origin "refs/tags/$tag" >/dev/null 2>&1; then
              echo "error: tag $tag already exists on origin" >&2
              exit 2
            fi

            if git show-ref --verify --quiet "refs/heads/$branch"; then
              echo "error: branch $branch already exists locally" >&2
              exit 2
            fi

            current_version=$(cat VERSION)
            if [ "$current_version" = "$version" ]; then
              echo "error: VERSION is already '$version'" >&2
              exit 2
            fi

            echo ">>> creating branch $branch"
            git checkout -b "$branch"
            echo "$version" > VERSION
            git add VERSION
            git commit -m "release: $tag"

            echo ">>> pushing $branch and opening PR"
            git push -u origin "$branch"

            gh pr create \
              --base main \
              --head "$branch" \
              --title "release: $tag" \
              --body "Bumps \`VERSION\` from \`$current_version\` to \`$version\`. Merging this PR will trigger the stable release workflow, which tags \`$tag\` and publishes a GitHub release with the built sdist + wheel."

            echo
            echo ">>> release PR opened. After it merges and Tests pass on main,"
            echo "    .github/workflows/release.yml will publish $tag."
          '';
        };
      });

      apps = forAllSystems ({ system, ... }: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/chud";
        };
      });

      formatter = forAllSystems ({ pkgs, ... }: pkgs.nixpkgs-fmt);
    };
}
