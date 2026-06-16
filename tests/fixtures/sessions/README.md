# Fixture Sessions

Locked paper session JSON files for capsule-replay CI.

**Source:** `papers/platform/sessions/*.json` (12 gauge sessions, locked 2026-06-10).

Copy session JSON files here to enable the nightly live replay job in
`.github/workflows/capsule-replay.yml`. Each file is named `<session_id>.json`.

The fixture job (`capsule-fixture`) runs without these files and only tests
the manifest/verify machinery. The live job (`capsule-live`) is skipped
automatically when this directory is empty.
