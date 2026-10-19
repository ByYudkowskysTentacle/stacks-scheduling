# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure domain core: the model, validation rules and solver model (CLAUDE.md §3).

Nothing in this package may import Django, a database driver or any web code, so
it can be unit-tested on its own and shared by the web app, the worker and the CLI.
"""
