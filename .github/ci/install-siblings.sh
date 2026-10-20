#!/usr/bin/env bash
# Install the sibling AI-Hydro packages that aihydro-tools requires, from pinned
# commits of their merged `main`, before `pip install -e .` runs.
#
# WHY GIT PINS, NOT PYPI (pending PyPI batch)
#   aihydro-tools main requires aihydro-watershed>=0.1.5, which is merged but not
#   yet on PyPI (PyPI has 0.1.0 and 0.1.4). Pip resolves a requirement that is
#   already satisfied by an installed package without consulting the index, so
#   installing these first makes `pip install -e .` reproducible and index-free
#   for the siblings. Every SHA below is a commit on the sibling repo's `main`.
#
# RETURN TO PYPI AFTER RELEASE
#   Once aihydro-watershed 0.1.6 (and the other pending releases) are published,
#   delete this script and its callers' "Install sibling packages" steps; the
#   version pins in pyproject.toml are then sufficient. To refresh a pin before
#   that, bump the SHA here (one place for all workflows).
#
# NOT INSTALLED HERE: aihydro-modelling and aihydro-lsh are private repositories
#   and cannot be cloned from public CI without a credential. The modelling extra
#   is therefore excluded from CI installs, and tests that need modelling/lsh are
#   skipped when the package is absent (see tests/_plugin_tools.py and the
#   `needs_modelling` markers). pygeoglim is a public supporting package.
set -euo pipefail

CORE_SHA=cebd299ba635621b6121ec8c3dd035355e5571ba        # aihydro-core main (v0.2.10)
DATA_SHA=873c329fd21700877de8e3fd717b7ccfe19ae241        # AIhydro-data main (v0.2.4)
WATERSHED_SHA=8868ace430dce493e26a050208a61c2d6e9a6f9a   # aihydro-watershed main (0.1.6, NumPy shim)
PYGEOGLIM_SHA=ebf4ae9e834ae6527309b36467fe6a6788dd135c   # pygeoglim main

python -m pip install --upgrade pip
python -m pip install \
  "aihydro-core[contracts] @ git+https://github.com/AI-Hydro/aihydro-core.git@${CORE_SHA}" \
  "aihydro-data[stac] @ git+https://github.com/AI-Hydro/AIhydro-data.git@${DATA_SHA}" \
  "aihydro-watershed @ git+https://github.com/AI-Hydro/aihydro-watershed.git@${WATERSHED_SHA}" \
  "pygeoglim @ git+https://github.com/galib9690/pygeoglim.git@${PYGEOGLIM_SHA}"
