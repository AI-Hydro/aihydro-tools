#!/usr/bin/env bash
# Install the sibling AI-Hydro packages that aihydro-tools requires, from pinned
# commits (merged `main`; watershed: its open PR branch, see below), before `pip install -e .` runs.
#
# WHY GIT PINS, NOT PYPI (pending PyPI batch)
#   aihydro-tools 2.8.0 requires aihydro-core>=0.2.11, aihydro-data>=0.2.5 and aihydro-watershed>=0.2.0
#   (Science Wave 3). None of the three is on PyPI yet (the owner publishes them first), so a plain
#   `pip install -e .` cannot resolve them. Pip resolves a requirement that is already satisfied by an
#   installed package without consulting the index, so installing these first makes `pip install -e .`
#   reproducible and index-free for the siblings. Only github.com is contacted (no new host).
#
#   core and data are pinned to the merged `main` commits that carry 0.2.11 / 0.2.5. watershed 0.2.0 is NOT
#   merged yet: its pin is the head of the open PR branch AI-Hydro/aihydro-watershed#4 (vision2040/w3-watershed,
#   reviewed PASS). When that PR is merged (squash changes the SHA) re-pin WATERSHED_SHA to the merge commit.
#
# RETURN TO PYPI AFTER RELEASE
#   Once aihydro-core 0.2.11, aihydro-data 0.2.5 and aihydro-watershed 0.2.0 are published, delete this script
#   and its callers' "Install sibling packages" steps; the version pins in pyproject.toml are then sufficient.
#   To refresh a pin before that, bump the SHA here (one place for all workflows).
#
# NOT INSTALLED HERE: aihydro-modelling and aihydro-lsh are private repositories
#   and cannot be cloned from public CI without a credential. The modelling extra
#   is therefore excluded from CI installs, and tests that need modelling/lsh are
#   skipped when the package is absent (see tests/_plugin_tools.py and the
#   `needs_modelling` markers). pygeoglim is a public supporting package.
set -euo pipefail

CORE_SHA=8331d194af2847dfdb9adff6e856ca51a7c550c4        # aihydro-core main (v0.2.11, grouped bootstrap, scope, water_year)
DATA_SHA=4a25b128357b223515da747dfc33ea1367bd5cee        # AIhydro-data main (v0.2.5, output validation, missing_ranges)
WATERSHED_SHA=6ba6615fff2e08a1e7f22c134d03696e456970ac   # aihydro-watershed PR #4 branch vision2040/w3-watershed (0.2.0, NOT yet on main)
PYGEOGLIM_SHA=ebf4ae9e834ae6527309b36467fe6a6788dd135c   # pygeoglim main

python -m pip install --upgrade pip
python -m pip install \
  "aihydro-core[contracts] @ git+https://github.com/AI-Hydro/aihydro-core.git@${CORE_SHA}" \
  "aihydro-data[stac] @ git+https://github.com/AI-Hydro/AIhydro-data.git@${DATA_SHA}" \
  "aihydro-watershed @ git+https://github.com/AI-Hydro/aihydro-watershed.git@${WATERSHED_SHA}" \
  "pygeoglim @ git+https://github.com/galib9690/pygeoglim.git@${PYGEOGLIM_SHA}"
