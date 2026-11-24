from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import (
    apply_corrections,
    correct_order,
    create_run,
    hold_order,
    issue_many,
    issue_order,
    present,
    release_order,
    replace_source,
    review_command,
    serialize,
)
from app.review.store import Store  # noqa: F401  (re-exported for type hints/tests)
from app.service import pipeline, pipeline_csv, pipeline_rows


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def _json():
    return request.get_json(silent=True) or {}


def _idempotency_key(data):
    return (request.headers.get("Idempotency-Key")
            or request.headers.get("X-Idempotency-Key")
            or data.get("idempotency_key"))


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = _json()
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


def _create_and_present(payload):
    created = create_run(store(), payload)
    return present(store(), store().get(created["run_id"]))


@api.post("/api/runs")
def create():
    uploaded = None
    if request.files:
        uploaded = request.files.get("file") or request.files.get("csv") \
            or request.files.get("upload") or next(iter(request.files.values()), None)
    if uploaded is not None and getattr(uploaded, "filename", None):
        payload = pipeline_csv(uploaded.read().decode("utf-8", "replace"),
                               filename=uploaded.filename or "upload.csv")
        return jsonify(_create_and_present(payload))

    data = _json()
    custom_rows = None
    for key in ("rows", "records", "items", "data_rows"):
        if data.get(key) is not None:
            custom_rows = data[key]
            break
    if custom_rows is not None:
        payload = pipeline_rows(custom_rows, filename=data.get("filename", "api-rows"))
    elif data.get("csv") is not None or data.get("content") is not None or data.get("text") is not None:
        payload = pipeline_csv(data.get("csv") if data.get("csv") is not None
                               else (data.get("content") if data.get("content") is not None
                                     else data.get("text")),
                               filename=data.get("filename", "upload.csv"))
    elif data.get("path"):
        payload = pipeline(data.get("profile", "clean"), data["path"])
    else:
        payload = pipeline(data.get("profile", "dirty"))
    return jsonify(_create_and_present(payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = present(store(), store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.get("/api/runs/<run_id>/review")
def get_review_state(run_id):
    run = present(store(), store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(status=run["status"], revision=run["revision"],
                   review_board=run["review_board"], orders=run["orders"],
                   order_reviews=run["order_reviews"], fulfillment=run.get("fulfillment"),
                   batch_hold=run["review_board"]["batch_hold"])


@api.put("/api/runs/<run_id>")
@api.post("/api/runs/<run_id>/replace")
def replace_run(run_id):
    data = _json()
    row, _payload = replace_source(
        store(), run_id, rows=data.get("rows", data.get("records")),
        csv_text=data.get("csv") or data.get("content"),
        expected_revision=data.get("expected_revision"))
    return jsonify(present(store(), row))


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = _json()
    result = review_command(store(), run_id, data, idempotency_key=_idempotency_key(data))
    return jsonify(result)


@api.post("/api/runs/<run_id>/review/<order_id>")
def review_order_by_path(run_id, order_id):
    data = _json()
    data.setdefault("order_id", order_id)
    result = review_command(store(), run_id, data, idempotency_key=_idempotency_key(data))
    return jsonify(result)


@api.post("/api/runs/<run_id>/orders/<order_id>/review")
def review_order_resource(run_id, order_id):
    data = _json()
    data.setdefault("order_id", order_id)
    result = review_command(store(), run_id, data, idempotency_key=_idempotency_key(data))
    return jsonify(result)


# ---------------------------------------------------------------------------
# Order-scoped batch review
# ---------------------------------------------------------------------------
@api.get("/api/runs/<run_id>/orders")
def list_orders(run_id):
    run = present(store(), store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    board = run["review_board"]
    return jsonify(orders=board["orders"], order_map=board["order_map"],
                   review_board=board, fulfillment=run.get("fulfillment"),
                   batch_hold=board["batch_hold"])


@api.get("/api/runs/<run_id>/orders/<order_id>")
def get_order(run_id, order_id):
    run = present(store(), store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    order = run["order_reviews"].get(order_id)
    if order is None:
        raise DomainIssue("ORDER_NOT_FOUND", 404)
    return jsonify(order)


@api.post("/api/runs/<run_id>/orders/<order_id>/issue")
def issue_order_route(run_id, order_id):
    data = _json()
    result = issue_order(
        store(), run_id, order_id,
        comment=data.get("comment", ""),
        expected_revision=data.get("expected_revision"),
        expected_amount_version=data.get("expected_amount_version", data.get("amount_version")),
        idempotency_key=_idempotency_key(data),
        amount=data.get("amount", data.get("pending_amount")))
    return jsonify(result)


@api.post("/api/runs/<run_id>/orders/<order_id>/hold")
def hold_order_route(run_id, order_id):
    data = _json()
    result = hold_order(store(), run_id, order_id, comment=data.get("comment", ""),
                        reason=data.get("reason"),
                        expected_revision=data.get("expected_revision"))
    return jsonify(result)


@api.post("/api/runs/<run_id>/orders/<order_id>/release")
def release_order_route(run_id, order_id):
    data = _json()
    result = release_order(store(), run_id, order_id,
                           expected_revision=data.get("expected_revision"))
    return jsonify(result)


@api.post("/api/runs/<run_id>/orders/<order_id>/correct")
@api.post("/api/runs/<run_id>/orders/<order_id>/rectify")
@api.post("/api/runs/<run_id>/orders/<order_id>/amend")
def correct_order_route(run_id, order_id):
    data = _json()
    data.setdefault("order_id", order_id)
    result = correct_order(store(), run_id, data,
                           and_issue=bool(data.get("and_issue") or data.get("issue_after")),
                           idempotency_key=_idempotency_key(data))
    return jsonify(result)


@api.post("/api/runs/<run_id>/orders/<order_id>/retry")
def retry_order_route(run_id, order_id):
    from app.review.service import retry_command
    data = _json()
    data.setdefault("order_id", order_id)
    result = retry_command(store(), run_id, data, idempotency_key=_idempotency_key(data))
    return jsonify(result)


# Flatter aliases (order id carried in the body).
@api.post("/api/runs/<run_id>/issue")
def issue_flat(run_id):
    data = _json()
    if not data.get("order_id") and not data.get("order"):
        raise DomainIssue("BAD_REQUEST")
    return jsonify(review_command(store(), run_id,
                                  {**data, "decision": "ISSUE"},
                                  idempotency_key=_idempotency_key(data)))


@api.post("/api/runs/<run_id>/hold")
def hold_flat(run_id):
    data = _json()
    if not data.get("order_id") and not data.get("order"):
        raise DomainIssue("BAD_REQUEST")
    return jsonify(review_command(store(), run_id,
                                  {**data, "decision": "HELD"}))


def _coerce_ops(data):
    """Accept list ops, a list body, or a ``{line_id: values}`` map."""
    body = request.get_json(silent=True)
    if isinstance(body, list):
        return body
    corrections = data.get("corrections")
    if isinstance(corrections, list):
        return corrections
    if isinstance(corrections, dict):
        ops = []
        for key, value in corrections.items():
            op = dict(value) if isinstance(value, dict) else {"values": value}
            key_name = "line" if str(key).isdigit() and int(key) >= 2 else "line_id"
            op.setdefault(key_name, int(key) if key_name == "line" else key)
            ops.append(op)
        return ops
    return None


@api.post("/api/runs/<run_id>/correct")
@api.post("/api/runs/<run_id>/corrections")
@api.post("/api/runs/<run_id>/rectify")
@api.post("/api/runs/<run_id>/amend")
def correct_flat(run_id):
    body = request.get_json(silent=True)
    # Bare JSON array of operations.
    if isinstance(body, list):
        row, _payload, _audit = apply_corrections(store(), run_id, body)
        return jsonify(present(store(), row))

    data = _json()
    # Wholesale corrected dataset.
    if data.get("rows") is not None or data.get("records") is not None \
            or data.get("csv") is not None or data.get("content") is not None:
        row, _payload = replace_source(
            store(), run_id, rows=data.get("rows", data.get("records")),
            csv_text=data.get("csv") or data.get("content"),
            expected_revision=data.get("expected_revision"))
        return jsonify(present(store(), row))
    if isinstance(data.get("corrections"), dict):
        ops = _coerce_ops(data)
        row, _payload, _audit = apply_corrections(
            store(), run_id, ops, expected_revision=data.get("expected_revision"))
        return jsonify(present(store(), row))
    if isinstance(data.get("corrections"), list):
        result = correct_order(store(), run_id, data,
                               and_issue=bool(data.get("and_issue")),
                               idempotency_key=_idempotency_key(data))
        return jsonify(result)

    payload = dict(data)
    # Alternate wrapper name for the corrected row.
    if "corrected_row" in payload and "values" not in payload:
        payload["values"] = payload["corrected_row"]
    result = correct_order(store(), run_id, payload,
                           and_issue=bool(payload.get("and_issue") or payload.get("issue_after")),
                           idempotency_key=_idempotency_key(data))
    return jsonify(result)


@api.post("/api/runs/<run_id>/issue-all")
@api.post("/api/runs/<run_id>/issue-eligible")
def issue_all_route(run_id):
    data = _json()
    order_ids = data.get("order_ids") or data.get("orders")
    result = issue_many(store(), run_id, order_ids, comment=data.get("comment", ""),
                        idempotency_key=_idempotency_key(data))
    return jsonify(result)


@api.post("/api/runs/<run_id>/retry")
def retry_flat(run_id):
    from app.review.service import retry_command
    data = _json()
    return jsonify(retry_command(store(), run_id, data,
                                 idempotency_key=_idempotency_key(data)))


@api.get("/api/runs/<run_id>/vouchers")
def list_vouchers(run_id):
    run = present(store(), store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    issued = store().list_issuances(run_id)
    vouchers = [{"order_id": row["order_id"], **__import__("json").loads(row["voucher"])}
                for row in issued]
    return jsonify(vouchers=vouchers)


@api.get("/api/runs/<run_id>/vouchers/<order_id>")
def get_voucher(run_id, order_id):
    row = store().get_issuance(run_id, order_id)
    if row is None:
        raise DomainIssue("VOUCHER_NOT_FOUND", 404)
    import json
    return jsonify(json.loads(row["voucher"]))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
