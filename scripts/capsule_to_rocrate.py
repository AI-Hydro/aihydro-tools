#!/usr/bin/env python3
"""Convert an existing AI-Hydro capsule into a capsule that also carries an RO-Crate.

    python scripts/capsule_to_rocrate.py INPUT_CAPSULE OUTPUT_DIR [--no-live] [--license SPDX]

Out of place: INPUT_CAPSULE is never modified (its file digests are compared before
and after), OUTPUT_DIR must not exist or must be empty. The copy gets bundle.json,
ro-crate-metadata.json, manifest-sha256.txt and the current replay.py. A capsule from
before claim revisions were carried reports claim_revisions as not_carried.
Exit code 0 when the crate was written and verifies, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys

from ai_hydro.capsule.rocrate_export import CrateExportError, convert_capsule


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--no-live", action="store_true", help="skip the run-log vs session cross-check")
    ap.add_argument("--license", default=None, help="SPDX id for the crate (default: none selected)")
    args = ap.parse_args(argv)
    try:
        result = convert_capsule(args.input, args.output, live=not args.no_live, license=args.license)
    except CrateExportError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
