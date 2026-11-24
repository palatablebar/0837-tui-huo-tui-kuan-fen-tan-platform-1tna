"""Order-scoped hold attribution and batch review board.

Rules implemented here (see docs/BUSINESS-RULES.md 批次复核):

* A bad row whose owning order can be recognised holds only that order.
* One product line claimed by two orders holds both orders.
* A bad row that cannot be attributed holds the whole batch.
* Corrections invalidate superseded pending amounts; issuance freezes an
  amount and a voucher and is idempotent per order.
"""

from app.intake.normalize import canonical

# Records that never represented a merchandise line and therefore must not
# hold an order.  Everything else is treated as a bad row for attribution.
BENIGN_CODES = {"BLANK_ROW", "REPEATED_HEADER", "EXACT_DUPLICATE"}

ORDER = "ORDER"
BATCH = "BATCH"

STATUS_READY = "READY"
STATUS_HELD = "HELD"
STATUS_BLOCKED = "BLOCKED"
STATUS_ISSUED = "ISSUED"

HELD_STATUSES = {STATUS_HELD}
OPEN_STATUSES = {STATUS_READY}


def _identity(value):
    text = canonical(value)
    if text in {"order_id", "line_id"}:
        return ""
    return text


def is_bad_row(issue):
    """True for issues that stand for an unusable merchandise row."""
    return bool(issue) and issue.get("code") not in BENIGN_CODES


def line_order_index(records):
    """Return ``(by_line_number, line_id -> sorted(order_ids))``."""
    by_line = {}
    line_orders = {}
    for record in records or []:
        line_no = record.get("line")
        if line_no is not None:
            by_line[line_no] = record
        if record.get("repeated"):
            continue
        cells = record.get("cells") or []
        order_id = _identity(cells[0]) if cells else ""
        line_id = _identity(cells[1]) if len(cells) > 1 else ""
        if line_id and order_id:
            line_orders.setdefault(line_id, set()).add(order_id)
    return by_line, {key: sorted(value) for key, value in line_orders.items()}


def _row_order_id(row):
    if not isinstance(row, dict):
        return ""
    return _identity(row.get("order_id"))


def attribute_issue(issue, records=None, line_orders=None, known_orders=None,
                    rows=None, by_line=None):
    """Resolve the set of order ids a single issue belongs to.

    The result always carries ``order_ids`` (possibly empty), ``attributable``
    and ``scope`` (``ORDER`` or ``BATCH``).
    """
    issue = issue or {}
    ids = []

    def add(value):
        text = _identity(value)
        if text and text not in ids:
            ids.append(text)

    add(issue.get("order_id"))
    for value in issue.get("order_ids") or []:
        add(value)
    for value in issue.get("orders") or []:
        add(value)

    key = issue.get("key") or issue.get("line_id") or ""
    line_no = issue.get("line")
    if line_no is None:
        line_no = issue.get("_line")

    if by_line is None:
        by_line, indexed_orders = line_order_index(records)
        if line_orders is None:
            line_orders = indexed_orders

    record = by_line.get(line_no) if line_no is not None else None
    if record is not None:
        cells = record.get("cells") or []
        add(cells[0] if cells else "")
        if not key and len(cells) > 1:
            key = cells[1]

    # Domain row dicts (tests / prepared rows) are an alternate context.
    if rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            same_line = line_no is not None and row.get("_line") == line_no
            same_key = key and row.get("line_id") == key
            if same_line or same_key:
                add(_row_order_id(row))

    # A product line claimed by several orders expands the scope to all.
    if key and line_orders and key in line_orders:
        for order_id in line_orders[key]:
            add(order_id)

    # COLUMN_COUNT issues historically carry the first cell (the order id
    # position) as their key; accept it when it names a known order.
    if not ids and key and known_orders and key in known_orders:
        add(key)

    order_ids = sorted(ids)
    return {
        "line": line_no,
        "key": key or "",
        "code": issue.get("code", ""),
        "order_id": order_ids[0] if order_ids else "",
        "order_ids": order_ids,
        "attributable": bool(order_ids),
        "scope": ORDER if order_ids else BATCH,
    }


def attribute_bad_rows(issues, records=None, line_orders=None, known_orders=None, rows=None):
    """Attribute every *bad* issue (benign ignored rows are filtered out)."""
    by_line, indexed = line_order_index(records)
    if line_orders is None:
        line_orders = indexed
    known = set(known_orders or [])
    return [
        attribute_issue(issue, line_orders=line_orders, known_orders=known,
                        rows=rows, by_line=by_line)
        for issue in issues or [] if is_bad_row(issue)
    ]


# Friendly alias used by some callers.
attribute_issues = attribute_bad_rows


def summarize_attribution(attributions):
    by_order = {}
    unattributable = []
    for item in attributions:
        if not item["attributable"]:
            unattributable.append(item)
            continue
        for order_id in item["order_ids"]:
            by_order.setdefault(order_id, []).append(item)
    return {
        "batch_hold": bool(unattributable),
        "hold_scope": BATCH if unattributable else (ORDER if by_order else None),
        "orders": by_order,
        "unattributable": unattributable,
    }


def _amount(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None


def build_board(records=None, issues=None, results=None, issued=None,
                manual_holds=None, previous_board=None, amount_meta=None):
    """Build the order-scoped review board for a run.

    ``issued`` maps order id -> frozen issuance/voucher snapshot.
    ``manual_holds`` maps order id -> hold metadata.
    ``previous_board`` lets correction recomputation invalidate stale quotes.
    """
    issues = issues or []
    results = results or []
    issued = issued or {}
    manual_holds = manual_holds or {}

    known_orders = set()
    result_by_order = {}
    for item in results:
        order_id = item.get("label") or item.get("order_id")
        if not order_id:
            continue
        order_id = str(order_id)
        known_orders.add(order_id)
        result_by_order[order_id] = item

    by_line, line_orders = line_order_index(records)
    for line_id, order_ids in line_orders.items():
        known_orders.update(order_ids)
    for issue in issues:
        if issue.get("order_id"):
            known_orders.add(issue["order_id"])
        known_orders.update(issue.get("order_ids") or [])
    known_orders.update(issued.keys())
    known_orders.update(manual_holds.keys())

    attributions = attribute_bad_rows(
        issues, line_orders=line_orders, known_orders=known_orders)
    summary = summarize_attribution(attributions)
    batch_hold = summary["batch_hold"]

    previous_by_id = {}
    if previous_board:
        for old in previous_board.get("orders", []):
            previous_by_id[old["order_id"]] = old

    meta = amount_meta or {}
    orders = []
    for order_id in sorted(known_orders):
        calc = result_by_order.get(order_id, {})
        related = summary["orders"].get(order_id, [])
        frozen = issued.get(order_id)
        manual = manual_holds.get(order_id)
        meta_entry = meta.get(order_id, {}) or {}

        computed_amount = _amount(calc.get("refund", calc.get("metric")))
        verdict = calc.get("verdict")
        reason = calc.get("reason")

        old = previous_by_id.get(order_id) or {}
        old_version = int(old.get("amount_version") or meta_entry.get("version") or 1)
        old_amount = old.get("pending_amount", old.get("amount"))
        old_amount = _amount(old_amount)

        changed = (old_amount is not None and computed_amount is not None
                   and old_amount != computed_amount and bool(old))
        if changed:
            amount_version = old_version + 1
            invalidated_amount = old_amount
        else:
            amount_version = int(meta_entry.get("version") or old_version or 1)
            invalidated_amount = meta_entry.get("invalidated_amount")
            invalidated_amount = _amount(invalidated_amount) if invalidated_amount is not None else None

        if frozen:
            status = STATUS_ISSUED
            amount = _amount(frozen.get("amount"))
            amount_version = int(frozen.get("amount_version") or amount_version or 1)
            pending_amount = amount
            invalidated_amount = None
        elif manual:
            status = STATUS_HELD
            amount = computed_amount
            pending_amount = computed_amount
        elif batch_hold:
            status = STATUS_HELD
            amount = computed_amount
            pending_amount = computed_amount
        elif related:
            status = STATUS_HELD
            amount = computed_amount
            pending_amount = computed_amount
        elif verdict == "REJECT":
            status = STATUS_BLOCKED
            amount = computed_amount
            pending_amount = computed_amount
        else:
            status = STATUS_READY
            amount = computed_amount
            pending_amount = computed_amount

        amount_stale = invalidated_amount is not None and not frozen

        reasons = []
        if batch_hold and not frozen:
            reasons.append("BATCH_HOLD")
        if manual and not frozen:
            reasons.append("MANUAL_HOLD")
        reasons.extend(attr["code"] for attr in related if attr.get("code"))
        if status == STATUS_BLOCKED and reason:
            reasons.append(reason)

        view = {
            "order_id": order_id,
            "id": order_id,
            "label": order_id,
            "status": status,
            "state": status,
            "issuable": status == STATUS_READY,
            "can_issue": status == STATUS_READY,
            "held": status == STATUS_HELD,
            "blocked": status == STATUS_BLOCKED,
            "issued": status == STATUS_ISSUED,
            "amount": amount,
            "refund": amount,
            "pending_amount": pending_amount,
            "amount_version": amount_version,
            "amount_stale": amount_stale,
            "stale": amount_stale,
            "quote_invalidated": amount_stale,
            "invalidated_amount": invalidated_amount,
            "previous_pending_amount": invalidated_amount,
            "old_pending_amount": invalidated_amount,
            "stale_amount": invalidated_amount,
            "verdict": verdict,
            "reason": reason,
            "reasons": list(dict.fromkeys(reasons)),
            "bad_rows": related,
            "issues": [{"line": a["line"], "key": a["key"], "code": a["code"],
                         "order_ids": a["order_ids"]} for a in related],
            "voucher": frozen.get("voucher") if frozen else None,
            "receipt": frozen.get("voucher") if frozen else None,
            "issuance": frozen,
            "issued_amount": _amount(frozen.get("amount")) if frozen else None,
            "manual_hold": manual,
            "recomputed_amount": computed_amount if frozen else None,
        }
        orders.append(view)

    order_map = {view["order_id"]: view for view in orders}
    counts = {"total": len(orders), STATUS_ISSUED: 0, STATUS_HELD: 0,
              STATUS_READY: 0, STATUS_BLOCKED: 0}
    for view in orders:
        counts[view["status"]] = counts.get(view["status"], 0) + 1

    board = {
        "batch_hold": batch_hold,
        "hold_scope": summary["hold_scope"],
        "batch_status": STATUS_HELD if batch_hold else "OPEN",
        "status": STATUS_HELD if batch_hold else "OPEN",
        "attributions": attributions,
        "unattributable": summary["unattributable"],
        "orders": orders,
        "order_map": order_map,
        "by_id": order_map,
        "counts": counts,
    }
    return board


# Plausible alternative entry-point names.
build_review_board = build_board
plan_batch_review = build_board


def held_order_ids(board):
    return [order["order_id"] for order in board["orders"] if order["status"] == STATUS_HELD]


def issuable_order_ids(board):
    return [order["order_id"] for order in board["orders"] if order["issuable"]]
