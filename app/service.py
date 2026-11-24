import csv
import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path

from app.catalog import SPEC
from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.intake.reader import read_records
from app.intake.service import adjudicate, load
from app.reconcile.ledger import build_ledger
from app.review.board import build_board


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _stage(name):
    return SPEC["stages"].index(name) + 1


def _default_source(intake, profile):
    if intake.get("path"):
        return {"profile": profile, "filename": Path(intake["path"]).name, "sha256": intake["sha256"]}
    return {"profile": profile, "filename": intake.get("filename", "api-input"),
            "sha256": intake.get("sha256", "")}


def assemble(intake, profile="dirty", upto="证据交付", source=None, run_key=None):
    prepared, prepare_issues = [], []
    results = []
    ledger = None
    if _stage(upto) >= 2:
        prepared, prepare_issues = prepare_rows(intake["rows"])
    if _stage(upto) >= 3:
        results = calculate_rows(prepared)
    if _stage(upto) >= 4:
        results = decide_rows(results)
    if _stage(upto) >= 5:
        ledger = build_ledger(intake, prepared, prepare_issues, results)
    intake["counts"]["prepared"] = len(prepared)
    intake["counts"]["computed"] = len(results)
    issues = intake["issues"] + prepare_issues
    summary = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for item in results:
        summary[item.get("verdict", "REVIEW")] = summary.get(item.get("verdict", "REVIEW"), 0) + 1
    chart_field = {"0840": "rolling", "0849": "mean", "0850": "settlement_cents"}.get(SPEC["code"], "metric")
    chart = [{"label": item.get("label", item.get(SPEC["key"], "")),
              "value": float(item.get(chart_field) or 0),
              "limit": float(item.get("limit") or 0)}
             for item in results[:12]]
    rows = []
    raw_stage = _stage(upto) <= 2
    if raw_stage:
        stage_rows = intake["rows"] if _stage(upto) == 1 else prepared
        rows = [{key: item.get(key) for key in SPEC["columns"]} for item in stage_rows[:18]]
    else:
        for item in results[:18]:
            rows.append({key: value for key, value in item.items()
                         if key in {"label", "verdict", "reason", "metric", "limit", "status", "state", "breakdown"}
                         or key in SPEC["columns"] or key in SPEC["result_columns"]})
    source = source or _default_source(intake, profile)
    board = build_board(records=intake.get("records", []), issues=issues, results=results)
    payload = {
        "project": SPEC["code"] + " " + SPEC["name"],
        "rule": SPEC["rule_short"],
        "profile": profile,
        "source": source,
        "counts": intake["counts"],
        "issues": issues[:60],
        "issues_all": issues,
        "rows": rows,
        "row_kind": "source" if raw_stage else "result",
        "chart": chart,
        "summary": summary,
        "ledger": ledger,
        # Full, untruncated engine state used by order-scoped review actions.
        "records": intake.get("records", []),
        "results_full": results,
        "review_board": board,
        "orders": board["orders"],
        "order_reviews": board["order_map"],
        "stages": [{"id": "intake", "name": SPEC["stages"][0]},
                   {"id": "prepare", "name": SPEC["stages"][1]},
                   {"id": "calculate", "name": SPEC["stages"][2]},
                   {"id": "decision", "name": SPEC["stages"][3]},
                   {"id": "reconcile", "name": SPEC["stages"][4]}],
        "run_key": run_key or hashlib.sha256(
            (SPEC["code"] + profile + intake["sha256"] + SPEC["rule"]).encode("utf-8")).hexdigest()[:24],
    }
    return jsonable(payload)


def pipeline(profile="dirty", path=None, upto="证据交付"):
    intake = load(profile, path)
    return assemble(intake, profile=profile, upto=upto)


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def records_from_rows(rows):
    """Build physical records from JSON row dicts or cell arrays."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(SPEC["columns"])
    for item in rows or []:
        if isinstance(item, dict):
            writer.writerow([_cell_text(item.get(column, "")) for column in SPEC["columns"]])
        else:
            writer.writerow([_cell_text(cell) for cell in item])
    raw = stream.getvalue().encode("utf-8")
    parsed = read_records(raw, SPEC["columns"], 5000)
    return parsed["records"], hashlib.sha256(raw).hexdigest()


def pipeline_rows(rows, profile="custom", filename="api-rows"):
    records, digest = records_from_rows(rows)
    intake = adjudicate(records)
    intake["sha256"] = digest
    source = {"profile": profile, "filename": filename, "sha256": digest}
    run_key = hashlib.sha256(
        (SPEC["code"] + "|rows|" + digest + SPEC["rule"]).encode("utf-8")).hexdigest()[:24]
    return assemble(intake, profile=profile, source=source, run_key=run_key)


def pipeline_csv(text, profile="upload", filename="upload.csv"):
    raw = (text or "").encode("utf-8")
    parsed = read_records(raw, SPEC["columns"], 5000)
    digest = hashlib.sha256(raw).hexdigest()
    intake = adjudicate(parsed["records"])
    intake["sha256"] = digest
    source = {"profile": profile, "filename": filename, "sha256": digest}
    run_key = hashlib.sha256(
        (SPEC["code"] + "|csv|" + digest + SPEC["rule"]).encode("utf-8")).hexdigest()[:24]
    return assemble(intake, profile=profile, source=source, run_key=run_key)


def evaluate_records(records, profile="corrected", source=None, run_key=None):
    """Re-run the full engine over corrected physical records."""
    intake = adjudicate(records)
    intake["sha256"] = (source or {}).get("sha256", "")
    return assemble(intake, profile=profile, source=source, run_key=run_key)


def payload_from_json(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    return json.loads(value) if isinstance(value, str) else value
