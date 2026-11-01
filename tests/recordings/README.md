<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

Only tiny synthetic shareable recordings belong here; keep local recordings (0600 in a 0700 folder) elsewhere, run `python3 tools/digital-twin/recording_lint.py promote <local.json>` to create a checked copy, and run `make -C tests/host recordings-lint` before adding it. The lint scans all sibling outputs for sentinel values; it cannot recognize personal text drawn as pixels or entered through key edges. `ping.json.license` supplies the JSON fixture's SPDX notice.
