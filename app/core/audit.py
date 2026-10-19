# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit log (CLAUDE.md §6): every write records who, when, before and after.

`pghistory` keeps a row snapshot in a database trigger on every insert, update and
delete, so bulk operations and raw SQL are captured too. The request middleware
(`pghistory.middleware.HistoryMiddleware`) attaches the signed-in user and the URL;
background jobs wrap their work in `pghistory.context(...)` themselves. "Before" is
the previous snapshot of the same row.
"""

from __future__ import annotations

import pghistory


def audited():
    """Class decorator: track inserts, updates and deletes for this model."""
    return pghistory.track(
        pghistory.InsertEvent(),
        pghistory.UpdateEvent(),
        pghistory.DeleteEvent(),
    )
