import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Keep in sync with `requires-python` in pyproject.toml. Guarding here (vs. in
# app.py/__main__.py) means *any* import path — entrypoint script, `python -m
# chud`, or `import chud` — fails fast with a clear message before submodules
# using 3.12-only syntax (e.g. `from datetime import UTC`) blow up cryptically.
_REQUIRED_PYTHON = (3, 12, 13)
if sys.version_info[:3] != _REQUIRED_PYTHON:
    _required = ".".join(map(str, _REQUIRED_PYTHON))
    _actual = ".".join(map(str, sys.version_info[:3]))
    sys.exit(
        f"chud requires Python {_required} but is running on {_actual}.\n"
        f"Reinstall with: "
        f"uv tool install --python {_required} git+https://github.com/Nixotica/chud"
    )

try:
    __version__ = _pkg_version("chud")
except PackageNotFoundError:  # not installed (rare: bare source checkout)
    __version__ = "0.0.0+unknown"
