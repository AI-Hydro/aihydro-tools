"""ai_hydro.__version__ is defined in source and must match pyproject.toml."""
import re
from pathlib import Path

import ai_hydro


def test_package_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
    assert ai_hydro.__version__ == declared
