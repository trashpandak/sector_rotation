"""Generic SCD Type 2 (slowly changing dimension) writer.

This is the single place that implements "close old row, open new row" logic so
that membership.py, index_engine, and any future SCD2 table all share one
correctness-critical implementation instead of five subtly different copies.

Guarantee: for a given (entity_key) there is at most one row with is_current=1
at any time, and effective_from/effective_to never overlap.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Set, Tuple

from rrg.common.logging_config import get_logger
from rrg.common.time_utils import day_before

logger = get_logger(__name__)


def sync_membership(
    conn: sqlite3.Connection,
    table: str,
    entity_col: str,
    group_col: str,
    as_of_date: str,
    desired_pairs: Set[Tuple[int, str]],
    group_scope: Set[str] | None = None,
) -> Tuple[int, int]:
    """Reconcile a many-to-many SCD2 table to exactly `desired_pairs` as of as_of_date,
    SCOPED to `group_scope` (the set of group_col values this call is authoritative for).

    This scoping matters whenever callers reconcile one group at a time (e.g. Index
    Construction Engine calling this once per index): without it, the "current state"
    read would span every group in the table, and closing "everything not desired"
    would incorrectly wipe out other groups' untouched, still-valid memberships.

    If group_scope is not given, it defaults to the groups present in desired_pairs --
    correct for callers (e.g. node/tag membership sync) that always pass the complete
    desired state across all groups in one call.

    desired_pairs: set of (entity_id, group_code) that SHOULD be current as of today,
    for every group in scope. Any currently-open pair whose group is in scope but the
    pair itself is not in desired_pairs is closed (effective_to = yesterday). Any pair
    in desired_pairs not currently open is opened (effective_from = today). Pairs
    already open and still desired are left untouched (no-op, preserves history).

    Returns (opened_count, closed_count).
    """
    if group_scope is None:
        group_scope = {group_code for _, group_code in desired_pairs}

    if not group_scope:
        return (0, 0)

    placeholders = ",".join("?" for _ in group_scope)
    cur = conn.execute(
        f"SELECT {entity_col}, {group_col} FROM {table} "
        f"WHERE is_current = 1 AND {group_col} IN ({placeholders})",
        tuple(group_scope),
    )
    current_pairs: Set[Tuple[int, str]] = {(row[0], row[1]) for row in cur.fetchall()}

    to_close = current_pairs - desired_pairs
    to_open = desired_pairs - current_pairs

    for entity_id, group_code in to_close:
        conn.execute(
            f"""UPDATE {table}
                SET effective_to = ?, is_current = 0
                WHERE {entity_col} = ? AND {group_col} = ? AND is_current = 1""",
            (day_before(as_of_date), entity_id, group_code),
        )

    for entity_id, group_code in to_open:
        conn.execute(
            f"""INSERT INTO {table} ({entity_col}, {group_col}, effective_from, is_current)
                VALUES (?, ?, ?, 1)""",
            (entity_id, group_code, as_of_date),
        )

    conn.commit()
    logger.info(
        "SCD2 sync on %s: opened=%d closed=%d unchanged=%d",
        table, len(to_open), len(to_close), len(current_pairs & desired_pairs),
    )
    return len(to_open), len(to_close)


def current_members(
    conn: sqlite3.Connection, table: str, entity_col: str, group_col: str, group_code: str
) -> list[int]:
    """Return entity ids currently (is_current=1) belonging to group_code."""
    cur = conn.execute(
        f"SELECT {entity_col} FROM {table} WHERE {group_col} = ? AND is_current = 1",
        (group_code,),
    )
    return [row[0] for row in cur.fetchall()]


def members_as_of(
    conn: sqlite3.Connection,
    table: str,
    entity_col: str,
    group_col: str,
    group_code: str,
    as_of_date: str,
) -> list[int]:
    """Point-in-time query: entities belonging to group_code as of as_of_date.
    This is what makes index reconstitution look-ahead-bias free."""
    cur = conn.execute(
        f"""SELECT {entity_col} FROM {table}
            WHERE {group_col} = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to >= ?)""",
        (group_code, as_of_date, as_of_date),
    )
    return [row[0] for row in cur.fetchall()]
