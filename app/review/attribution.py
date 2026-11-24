"""Compatibility facade for bad-row attribution / hold planning.

The implementation lives in :mod:`app.review.board`; this module exposes the
same pure rules under the vocabulary used in the batch-review workflow.
"""

from app.review.board import (  # noqa: F401
    BATCH,
    ORDER,
    attribute_bad_rows,
    attribute_issues,
    attribute_issue,
    build_board,
    build_review_board,
    held_order_ids,
    is_bad_row,
    issuable_order_ids,
    line_order_index,
    summarize_attribution,
)


def attribute(issues, records=None, **kwargs):
    return attribute_bad_rows(issues, records=records, **kwargs)


def find_owners(issue, records=None, **kwargs):
    return attribute_issue(issue, records=records, **kwargs).get("order_ids", [])


def resolve_scope(issues, records=None, **kwargs):
    attributions = attribute_bad_rows(issues, records=records, **kwargs)
    summary = summarize_attribution(attributions)
    return BATCH if summary["batch_hold"] else ORDER


def plan_holds(issues=None, records=None, results=None, **kwargs):
    """Return ``{order_id: 'HELD'|'BLOCKED'|'READY'|'ISSUED'}`` and the board."""
    board = build_board(records=records, issues=issues, results=results, **kwargs)
    return {oid: view["status"] for oid, view in board["order_map"].items()}, board


def order_hold_plan(*args, **kwargs):
    return plan_holds(*args, **kwargs)
