import copy
import json
import uuid
from datetime import datetime, timezone

from app.catalog import SPEC
from app.errors import DomainIssue
from app.evidence.manifest import stable_digest
from app.review.board import (
    STATUS_BLOCKED,
    STATUS_HELD,
    STATUS_ISSUED,
    STATUS_READY,
    attribute_bad_rows,
    build_board,
    is_bad_row,
)
from app.review.policy import approval_allowed
from app.service import evaluate_records


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409, extra={"board": _board_snapshot(store, row)})
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")
    row, problem = store.review(run_id, decision, (comment or "").strip(), expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return serialize(row)


def serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def _issuance_dict(row):
    return {
        "order_id": row["order_id"],
        "amount": row["amount"],
        "amount_version": row["amount_version"],
        "voucher": json.loads(row["voucher"]),
        "idempotency_key": row["idempotency_key"],
        "revision": row["revision"],
        "created_at": row["created_at"],
    }


def issued_map(store, run_id):
    return {row["order_id"]: _issuance_dict(row) for row in store.list_issuances(run_id)}


def _board_from_payload(payload, issued, previous_board=None):
    return build_board(
        records=payload.get("records", []),
        issues=payload.get("issues_all") or payload.get("issues", []),
        results=payload.get("results_full", []),
        issued=issued,
        manual_holds=payload.get("manual_holds", {}),
        previous_board=previous_board if previous_board is not None else payload.get("review_board"),
        amount_meta=payload.get("amount_meta", {}),
    )


def current_board(store, row, payload=None):
    payload = payload or json.loads(row["payload"])
    return _board_from_payload(payload, issued_map(store, row["id"]))


def _fulfillment(board):
    orders = board["orders"]
    issued = [o["order_id"] for o in orders if o["status"] == STATUS_ISSUED]
    held = [o["order_id"] for o in orders if o["status"] == STATUS_HELD]
    blocked = [o["order_id"] for o in orders if o["status"] == STATUS_BLOCKED]
    ready = [o["order_id"] for o in orders if o["status"] == STATUS_READY]
    return {
        "batch_hold": board["batch_hold"],
        "all_issued": bool(orders) and len(issued) == len(orders),
        "issued_orders": issued,
        "held_orders": held,
        "blocked_orders": blocked,
        "ready_orders": ready,
        "counts": board["counts"],
    }


def present(store, row):
    """Serialize a run with a freshly reconciled review board."""
    if row is None:
        return None
    payload = json.loads(row["payload"])
    board = current_board(store, row, payload)
    payload["review_board"] = board
    payload["orders"] = board["orders"]
    payload["order_reviews"] = board["order_map"]
    payload["order_map"] = board["order_map"]
    payload["by_id"] = board["order_map"]
    payload["order_status"] = {oid: view["status"] for oid, view in board["order_map"].items()}
    payload["order_holds"] = {oid: view["reasons"] for oid, view in board["order_map"].items()
                              if view["status"] == STATUS_HELD}
    fulfillment = _fulfillment(board)
    payload["fulfillment"] = fulfillment
    for key in ("held_orders", "blocked_orders", "ready_orders", "issued_orders"):
        payload[key] = fulfillment[key]
    payload["batch_hold"] = board["batch_hold"]
    payload["issuances"] = [_issuance_dict(item) for item in store.list_issuances(row["id"])]
    fresh_row = dict(row)
    fresh_row["payload"] = json.dumps(payload, ensure_ascii=False)
    return serialize(fresh_row)


def _board_snapshot(store, row):
    try:
        board = current_board(store, row)
        return {"batch_hold": board["batch_hold"], "counts": board["counts"]}
    except Exception:  # pragma: no cover - defensive error detail
        return None


def _action_body(store, row, order_id, top_status, *, idempotent=False, extra=None):
    run = present(store, row)
    order = run["order_reviews"].get(str(order_id))
    body = dict(run)
    # Order-scoped endpoints report the *order* status at top level while the
    # full run (with legacy run status) stays available under ``run``.
    body["status"] = top_status
    body["order_status"] = top_status
    body["order_id"] = str(order_id)
    body["order"] = order
    if order:
        body["voucher"] = order.get("voucher")
        body["issuance"] = order.get("issuance")
    body["idempotent"] = idempotent
    body["duplicate"] = idempotent
    body["run_status"] = run.get("status")
    body["run"] = run
    if extra:
        body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------
def _make_voucher(payload, order_id, amount, amount_version, comment, idempotency_key):
    voucher_id = "VCH-" + uuid.uuid4().hex[:16].upper()
    issued_at = datetime.now(timezone.utc).isoformat()
    voucher = {
        "schema": "workbench-voucher/v1",
        "voucher_id": voucher_id,
        "id": voucher_id,
        "voucher_no": voucher_id,
        "run_id": payload.get("run_id"),
        "order_id": str(order_id),
        "amount": int(amount),
        "amount_cents": int(amount),
        "currency": "CNY",
        "unit": "cent",
        "amount_version": int(amount_version or 1),
        "source": payload.get("source"),
        "rule": payload.get("rule"),
        "comment": (comment or "").strip(),
        "idempotency_key": idempotency_key,
        "status": STATUS_ISSUED,
        "decision": "APPROVED",
        "issued_at": issued_at,
        "created_at": issued_at,
    }
    digest = stable_digest({key: value for key, value in voucher.items()
                            if key not in {"voucher_sha256", "sha256"}})
    voucher["voucher_sha256"] = digest
    voucher["sha256"] = digest
    return voucher


def issue_order(store, run_id, order_id, *, comment="", expected_revision=None,
                expected_amount_version=None, idempotency_key=None, amount=None,
                **_ignored):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    order_id = str(order_id)

    # Exactly-once: a retry always returns the previously frozen voucher,
    # independently of the (possibly stale) optimistic revision.
    existing = store.get_issuance(run_id, order_id)
    if existing is not None:
        return _action_body(store, row, order_id, STATUS_ISSUED, idempotent=True)
    if idempotency_key:
        replayed = store.find_issuance_by_key(run_id, idempotency_key)
        if replayed is not None:
            if replayed["order_id"] != order_id:
                raise DomainIssue("IDEMPOTENCY_CONFLICT", 409)
            return _action_body(store, row, order_id, STATUS_ISSUED, idempotent=True)

    payload = json.loads(row["payload"])
    board = current_board(store, row, payload)
    view = board["order_map"].get(order_id)
    if view is None:
        raise DomainIssue("ORDER_NOT_FOUND", 404, extra={"order_id": order_id})
    if board["batch_hold"]:
        raise DomainIssue("BATCH_HELD", 409, extra={"order": view})
    if view["status"] == STATUS_HELD:
        raise DomainIssue("ORDER_HELD", 409, extra={"order": view})
    if view["status"] == STATUS_BLOCKED:
        raise DomainIssue("ORDER_BLOCKED", 409, extra={"order": view})
    if not view["issuable"]:
        raise DomainIssue("ORDER_HELD", 409, extra={"order": view})

    current_amount = view["amount"]
    if current_amount is None:
        raise DomainIssue("AMOUNT_UNAVAILABLE", 409, extra={"order": view})
    if expected_amount_version is not None:
        try:
            wanted = int(expected_amount_version)
        except (TypeError, ValueError):
            wanted = -1
        if wanted != int(view["amount_version"]):
            raise DomainIssue("STALE_AMOUNT", 409, extra={"order": view})
    if amount is not None:
        try:
            if int(amount) != int(current_amount):
                raise DomainIssue("STALE_AMOUNT", 409, extra={"order": view})
        except (TypeError, ValueError):
            raise DomainIssue("BAD_REQUEST")

    voucher = _make_voucher(payload, order_id, current_amount, view["amount_version"],
                            comment, idempotency_key)
    issuance = {
        "order_id": order_id,
        "amount": int(current_amount),
        "amount_version": int(view["amount_version"]),
        "voucher": voucher,
        "idempotency_key": idempotency_key,
    }
    issued = issued_map(store, run_id)
    issued[order_id] = issuance
    new_board = _board_from_payload(payload, issued, previous_board=board)
    payload["review_board"] = new_board
    payload["orders"] = new_board["orders"]
    payload["order_reviews"] = new_board["order_map"]
    payload.setdefault("vouchers", []).append({
        "order_id": order_id, "voucher_id": voucher["voucher_id"],
        "amount": voucher["amount"], "voucher_sha256": voucher["voucher_sha256"],
    })

    insertion, problem = store.insert_issuance(
        run_id, order_id, current_amount, view["amount_version"], voucher,
        idempotency_key, payload, expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    if problem == "exists":
        fresh = store.get(run_id)
        return _action_body(store, fresh, insertion["order_id"], STATUS_ISSUED, idempotent=True)

    fresh = store.get(run_id)
    return _action_body(store, fresh, order_id, STATUS_ISSUED, idempotent=False)


# ---------------------------------------------------------------------------
# Manual hold / release
def issue_many(store, run_id, order_ids=None, *, comment="", idempotency_key=None, **kwargs):
    """Issue several READY orders (or every issuable order) idempotently."""
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    board = current_board(store, row)
    if order_ids:
        targets = [str(oid) for oid in order_ids]
    else:
        targets = [oid for oid, view in board["order_map"].items() if view["issuable"]]
    issued, skipped = [], []
    for index, order_id in enumerate(targets):
        view = board["order_map"].get(order_id)
        if view is None:
            skipped.append({"order_id": order_id, "code": "ORDER_NOT_FOUND"})
            continue
        if view["status"] == STATUS_ISSUED or store.get_issuance(run_id, order_id):
            issued.append(order_id)
            continue
        if not view["issuable"]:
            skipped.append({"order_id": order_id, "status": view["status"],
                            "reasons": view["reasons"]})
            continue
        per_key = f"{idempotency_key}:{order_id}" if idempotency_key else None
        issue_order(store, run_id, order_id, comment=comment, idempotency_key=per_key)
        issued.append(order_id)
    fresh = store.get(run_id)
    body = present(store, fresh)
    body["issued_orders"] = issued
    body["skipped_orders"] = skipped
    body["all_issued"] = body["fulfillment"]["all_issued"]
    return body


# Plausible short aliases.
issue = issue_order
sign_order = issue_order


# ---------------------------------------------------------------------------
# Manual hold / release
# ---------------------------------------------------------------------------
def hold_order(store, run_id, order_id, *, comment="", reason=None,
               expected_revision=None, **_ignored):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    order_id = str(order_id)
    if store.get_issuance(run_id, order_id) is not None:
        raise DomainIssue("ORDER_ALREADY_ISSUED", 409)
    payload = json.loads(row["payload"])
    board = current_board(store, row, payload)
    if order_id not in board["order_map"]:
        raise DomainIssue("ORDER_NOT_FOUND", 404, extra={"order_id": order_id})
    payload.setdefault("manual_holds", {})[order_id] = {
        "comment": (comment or "").strip(),
        "reason": reason or "MANUAL_HOLD",
        "held_at": datetime.now(timezone.utc).isoformat(),
    }
    new_board = _board_from_payload(payload, issued_map(store, run_id), previous_board=board)
    payload["review_board"] = new_board
    payload["orders"] = new_board["orders"]
    payload["order_reviews"] = new_board["order_map"]
    row2, problem = store.update_payload(run_id, payload, expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return _action_body(store, row2, order_id, STATUS_HELD)


def release_order(store, run_id, order_id, *, expected_revision=None, **_ignored):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    order_id = str(order_id)
    payload = json.loads(row["payload"])
    holds = payload.setdefault("manual_holds", {})
    if order_id in holds:
        del holds[order_id]
    board = current_board(store, row, payload)
    new_board = _board_from_payload(payload, issued_map(store, run_id), previous_board=board)
    payload["review_board"] = new_board
    payload["orders"] = new_board["orders"]
    payload["order_reviews"] = new_board["order_map"]
    row2, problem = store.update_payload(run_id, payload, expected_revision)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return _action_body(store, row2, order_id, new_board["order_map"].get(order_id, {}).get("status", STATUS_READY))


# ---------------------------------------------------------------------------
# Correction / rectification
# ---------------------------------------------------------------------------
COLUMNS = SPEC["columns"]


def _canonical_cell(value):
    from app.intake.normalize import canonical
    return canonical("" if value is None else (value if isinstance(value, str) else str(value)))


def _extract_values(data):
    for key in ("values", "row", "fields", "correction", "data", "changes", "patch"):
        value = data.get(key)
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if v is not None}
    return {column: data[column] for column in COLUMNS
            if column in data and data[column] is not None}


def _resolve_record(records, *, line=None, line_id=None, order_id=None):
    if line is not None and line != "":
        try:
            line_no = int(line)
        except (TypeError, ValueError):
            line_no = None
        if line_no is not None:
            for record in records:
                if record.get("line") == line_no:
                    return record
    candidates = list(records)
    if line_id:
        line_id = str(line_id)
        candidates = [r for r in candidates
                      if len(r.get("cells") or []) > 1 and str(r["cells"][1]) == line_id]
    if order_id:
        order_id = str(order_id)
        with_order = [r for r in candidates if (r.get("cells") or [""])[0] == order_id]
        if with_order:
            candidates = with_order
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer an exact order+line match; otherwise surface ambiguity.
        if order_id and line_id:
            exact = [r for r in candidates
                     if (r.get("cells") or [""])[0] == str(order_id)
                     and len(r.get("cells") or []) > 1 and r["cells"][1] == str(line_id)]
            if len(exact) == 1:
                return exact[0]
        raise DomainIssue("CORRECTION_TARGET_NOT_FOUND", 409,
                          detail="同一商品行匹配到多条坏行，请用 source line 精确定位或批量补正")
    return None


def _next_line(records):
    return max((int(r.get("line") or 0) for r in records), default=1) + 1


def _apply_op(records, op):
    """Apply one correction op; returns ``(target_line, action)``."""
    line = op.get("line", op.get("line_no"))
    line_id = op.get("line_id", op.get("key"))
    order_id = op.get("order_id", op.get("order"))
    removing = bool(op.get("remove", op.get("delete", op.get("deleted"))))
    values = _extract_values(op)

    record = _resolve_record(records, line=line, line_id=line_id, order_id=order_id)

    if removing:
        if record is None:
            raise DomainIssue("CORRECTION_TARGET_NOT_FOUND", 404)
        target_line = record["line"]
        records.remove(record)
        return target_line, "removed"

    if record is None:
        # A correction may supply a missing merchandise line wholesale.
        if not values or not (values.get("line_id") or line_id):
            raise DomainIssue("CORRECTION_TARGET_NOT_FOUND", 404,
                              extra={"line_id": line_id, "line": line})
        cells = [_canonical_cell(values.get(column, "")) for column in COLUMNS]
        record = {"line": _next_line(records), "cells": cells,
                  "blank": False, "repeated": False}
        records.append(record)
        target_line = record["line"]
    else:
        target_line = record["line"]
        cells = list(record.get("cells") or [])
        if isinstance(values.get("cells"), list):
            cells = [_canonical_cell(cell) for cell in values["cells"]]
        else:
            # Re-align to the canonical column width; this drops surplus
            # trailing cells that trigger COLUMN_COUNT and pads short rows.
            aligned = (cells + [""] * len(COLUMNS))[:len(COLUMNS)]
            for column, value in values.items():
                if column in COLUMNS:
                    aligned[COLUMNS.index(column)] = _canonical_cell(value)
            cells = aligned
        record["cells"] = cells

    header = list(COLUMNS)
    record["cells"] = [_canonical_cell(cell) for cell in record["cells"]]
    record["blank"] = not any(record["cells"])
    record["repeated"] = record["cells"] == header
    return target_line, "corrected"


def apply_corrections(store, run_id, ops, *, expected_revision=None, comment=None,
                      persist=True):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if "records" not in payload:
        raise DomainIssue("CORRECTION_TARGET_NOT_FOUND", 409,
                          detail="该批次未保留来源物理行，无法补正")

    old_board = current_board(store, row, payload)
    records = copy.deepcopy(payload["records"])
    target_lines = set()
    audit = []
    for raw_op in ops or []:
        if not isinstance(raw_op, dict):
            raise DomainIssue("BAD_REQUEST")
        op = dict(raw_op)
        nested = _extract_values(op)
        # Identifiers may live either on the op or inside the values patch.
        for target, candidate in (
            ("line", nested.get("line") or nested.get("line_no")),
            ("line_id", nested.get("line_id") or nested.get("key")),
            ("order_id", nested.get("order_id") or nested.get("order")),
        ):
            if not op.get(target) and candidate:
                op[target] = candidate
        # An order-scoped correction with no explicit line targets its only
        # currently attributed bad line.
        if not op.get("line") and not op.get("line_id") and op.get("order_id"):
            view = old_board["order_map"].get(str(op["order_id"]))
            bad_lines = []
            if view:
                bad_lines = [item.get("line") for item in view.get("bad_rows", [])
                             if item.get("line") is not None]
            bad_lines = sorted(set(bad_lines))
            if len(bad_lines) == 1:
                op["line"] = bad_lines[0]
        target_line, action = _apply_op(records, op)
        target_lines.add(target_line)
        audit.append({"line": target_line, "action": action,
                      "order_id": op.get("order_id"), "at": datetime.now(timezone.utc).isoformat()})

    candidate = evaluate_records(records, profile=payload.get("profile", "corrected"),
                                 source=payload.get("source"), run_key=payload.get("run_key"))
    remaining = [issue for issue in candidate.get("issues_all", [])
                 if issue.get("line") in target_lines and is_bad_row(issue)]
    if remaining:
        raise DomainIssue("CORRECTION_FAILED", 422,
                          extra={"issues": remaining, "target_lines": sorted(target_lines)})

    # Recompute board while preserving frozen issuances and manual holds and
    # invalidating superseded pending amounts via the previous board.
    issued = issued_map(store, run_id)
    _finalize_candidate(payload, candidate, issued, old_board, audit=audit)

    if not persist:
        return row, candidate, audit

    row2, problem = store.update_payload(run_id, candidate, expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return row2, candidate, audit


def _finalize_candidate(payload, candidate, issued, old_board, audit=None):
    new_board = _board_from_payload(candidate, issued, previous_board=old_board)
    amount_meta = copy.deepcopy(payload.get("amount_meta", {}))
    for view in new_board["orders"]:
        if view["status"] == STATUS_ISSUED:
            continue
        entry = amount_meta.setdefault(view["order_id"], {})
        history = entry.setdefault("history", [])
        if view.get("invalidated_amount") is not None and (
                not history or history[-1].get("amount") != view["invalidated_amount"]
                or history[-1].get("amount_version") != view["amount_version"] - 1):
            history.append({
                "amount": view["invalidated_amount"],
                "superseded_by": view["pending_amount"],
                "amount_version": view["amount_version"] - 1,
                "invalidated_at": datetime.now(timezone.utc).isoformat(),
            })
        entry.update({"version": view["amount_version"],
                      "pending_amount": view["pending_amount"],
                      "invalidated_amount": view.get("invalidated_amount")})
    candidate["amount_meta"] = amount_meta
    candidate["manual_holds"] = payload.get("manual_holds", {})
    candidate["review_board"] = new_board
    candidate["orders"] = new_board["orders"]
    candidate["order_reviews"] = new_board["order_map"]
    candidate["order_map"] = new_board["order_map"]
    candidate["fulfillment"] = _fulfillment(new_board)
    if audit:
        candidate["corrections"] = payload.get("corrections", []) + audit
    elif payload.get("corrections"):
        candidate["corrections"] = payload["corrections"]
    if payload.get("vouchers"):
        candidate["vouchers"] = payload["vouchers"]
    return candidate


def replace_source(store, run_id, *, rows=None, csv_text=None, expected_revision=None):
    """Wholesale replacement of source rows with a corrected complete set."""
    from app.service import pipeline_csv, records_from_rows
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    old_board = current_board(store, row, payload)
    if rows is not None:
        records, _digest = records_from_rows(rows)
    elif csv_text is not None:
        from app.intake.reader import read_records
        from app.catalog import SPEC as _SPEC
        import hashlib
        raw = csv_text.encode("utf-8")
        records = read_records(raw, _SPEC["columns"], 5000)["records"]
        _digest = hashlib.sha256(raw).hexdigest()
    else:
        raise DomainIssue("BAD_REQUEST")
    candidate = evaluate_records(records, profile=payload.get("profile", "corrected"),
                                 source=payload.get("source"), run_key=payload.get("run_key"))
    issued = issued_map(store, run_id)
    _finalize_candidate(payload, candidate, issued, old_board,
                        audit=[{"action": "replace_source",
                                "at": datetime.now(timezone.utc).isoformat()}])
    row2, problem = store.update_payload(run_id, candidate, expected_revision)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return row2, candidate


def correct_order(store, run_id, data=None, *, and_issue=False, idempotency_key=None):
    data = dict(data or {})
    expected_revision = data.get("expected_revision")
    ops = data.get("corrections") if isinstance(data.get("corrections"), list) else None
    if ops is None:
        ops = [data]
    row2, candidate, audit = apply_corrections(
        store, run_id, ops, expected_revision=expected_revision,
        comment=data.get("comment"))

    order_id = data.get("order_id") or data.get("order")
    if not order_id:
        # Best effort: the corrected physical line's first cell.
        for item in audit:
            line = item.get("line")
            record = next((r for r in candidate.get("records", []) if r.get("line") == line), None)
            if record and record.get("cells"):
                order_id = record["cells"][0]
                break
    result = None
    if and_issue and order_id:
        result = issue_order(store, run_id, order_id, comment=data.get("comment", ""),
                             idempotency_key=idempotency_key or data.get("idempotency_key"),
                             expected_amount_version=data.get("expected_amount_version")
                             or data.get("amount_version"))
        return result
    fresh = store.get(run_id)
    run = present(store, fresh)
    order = None
    top_status = run.get("status")
    if order_id and str(order_id) in run["order_reviews"]:
        order = run["order_reviews"][str(order_id)]
        top_status = order["status"]
    body = dict(run)
    body["status"] = top_status
    body["order_status"] = top_status
    body["order_id"] = str(order_id) if order_id else None
    body["order"] = order
    body["correction"] = audit
    body["corrections"] = candidate.get("corrections", [])
    body["run_status"] = run.get("status")
    body["run"] = run
    return body


def retry_command(store, run_id, data=None, *, idempotency_key=None):
    data = dict(data or {})
    order_id = data.get("order_id") or data.get("order")
    value_containers = ("values", "row", "fields", "correction", "changes", "patch", "cells")
    editable_columns = [column for column in COLUMNS if column not in {"order_id", "line_id"}]
    has_correction = (
        any(key in data for key in value_containers)
        or bool(data.get("line") or data.get("line_no") or data.get("remove") or data.get("delete"))
        or any(column in data for column in editable_columns)
    )
    if has_correction:
        return correct_order(store, run_id, data, and_issue=True,
                             idempotency_key=idempotency_key or data.get("idempotency_key"))
    if not order_id:
        raise DomainIssue("BAD_REQUEST")
    return issue_order(store, run_id, order_id, comment=data.get("comment", ""),
                       expected_revision=data.get("expected_revision"),
                       expected_amount_version=data.get("expected_amount_version")
                       or data.get("amount_version"),
                       idempotency_key=idempotency_key or data.get("idempotency_key"))


# ---------------------------------------------------------------------------
# Dispatcher used by the extended /review endpoint
# ---------------------------------------------------------------------------
def review_command(store, run_id, data, *, idempotency_key=None):
    order_id = (data.get("order_id") or data.get("order") or data.get("order_no")
                or data.get("target_order"))
    decision = (data.get("decision") or data.get("action") or "").upper()

    # Bulk issuance of several (or all eligible) orders.
    order_ids = data.get("order_ids") or data.get("orders")
    if not order_id and isinstance(order_ids, list) and decision in {
            "APPROVED", "ISSUE", "ISSUED", "SIGN"}:
        return issue_many(store, run_id, [str(oid) for oid in order_ids],
                          comment=data.get("comment", ""), idempotency_key=idempotency_key)
    if not order_id and not order_ids and decision in {"ISSUE_ALL", "APPROVE_READY"}:
        return issue_many(store, run_id, None, comment=data.get("comment", ""),
                          idempotency_key=idempotency_key)

    if not order_id:
        if "decision" not in data:
            raise DomainIssue("BAD_REQUEST")
        if "expected_revision" not in data:
            raise DomainIssue("BAD_REQUEST")
        return approve(store, run_id, data["decision"], data.get("comment", ""),
                        int(data["expected_revision"]))

    decision = (data.get("decision") or data.get("action") or "").upper()
    revision = data.get("expected_revision")
    if revision is not None:
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            raise DomainIssue("BAD_REQUEST")
    amount_version = data.get("expected_amount_version", data.get("amount_version",
                                                                data.get("version")))
    kwargs = dict(comment=data.get("comment", ""), expected_revision=revision)
    if decision in {"HELD", "HOLD", "SUSPEND", "DEFER"}:
        return hold_order(store, run_id, order_id, reason=data.get("reason"), **kwargs)
    if decision in {"RELEASE", "UNHOLD", "RESUME"}:
        return release_order(store, run_id, order_id, expected_revision=revision)
    if decision in {"CORRECT", "CORRECTED", "RECTIFY", "FIX", "AMEND"}:
        return correct_order(store, run_id, data)
    if decision in {"RETRY", "RETRY_ISSUE"}:
        return retry_command(store, run_id, data, idempotency_key=idempotency_key)
    # APPROVED / ISSUE / ISSUED / SIGN and the default all mean issue.
    return issue_order(store, run_id, order_id,
                       expected_amount_version=amount_version,
                       idempotency_key=idempotency_key or data.get("idempotency_key"),
                       amount=data.get("amount") or data.get("pending_amount"),
                       **kwargs)


# Late-bound aliases for alternative naming conventions.
rectify = correct_order
apply_correction = correct_order
issue_for_retry = retry_command

# Additional vocabulary aliases used by some batch-review designs.
correct_run = correct_order
rectify_run = correct_order
review_retry = retry_command
issue_for_orders = issue_many
plan_order_holds = build_board
attribute_orders = attribute_bad_rows


def get_review_board(store, run_id):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return current_board(store, row)
