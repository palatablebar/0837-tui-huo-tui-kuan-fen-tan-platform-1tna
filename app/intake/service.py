from collections import OrderedDict
from pathlib import Path

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.intake.limits import MAX_FILE, MAX_ROWS
from app.intake.reader import read_records


def _path(profile: str, path: str | None):
    if path:
        return Path(path)
    if profile not in {"clean", "dirty"}:
        raise DomainIssue("SAMPLE_UNKNOWN")
    return SAMPLES / f"{profile}.csv"


def _cell(cells, index):
    return cells[index] if len(cells) > index else ""


def adjudicate(parsed_records):
    """Turn parsed physical records into accepted rows, issues and counts.

    This is the pure half of :func:`load`, split out so a corrected set of
    physical records can be re-adjudicated without going through a file.
    """
    issues = []
    counts = {"input": len(parsed_records), "accepted": 0, "isolated": 0, "duplicates": 0, "ignored": 0}
    valid = []
    for record in parsed_records:
        cells = record["cells"]
        if record["blank"]:
            counts["ignored"] += 1
            issues.append({"line": record["line"], "key": "", "code": "BLANK_ROW",
                           "order_id": "", "order_ids": []})
        elif record["repeated"]:
            counts["ignored"] += 1
            issues.append({"line": record["line"], "key": "", "code": "REPEATED_HEADER",
                           "order_id": "", "order_ids": []})
        elif len(cells) != len(SPEC["columns"]):
            counts["isolated"] += 1
            order_id = _cell(cells, 0)
            line_id = _cell(cells, 1)
            # For column-count rows the first cell still sits in the
            # ``order_id`` position, so ownership can often be recognised.
            issues.append({"line": record["line"], "key": cells[0] if cells else "",
                           "code": "COLUMN_COUNT", "order_id": order_id,
                           "order_ids": [order_id] if order_id else [],
                           "line_id": line_id})
        else:
            row = dict(zip(SPEC["columns"], cells))
            row["_line"] = record["line"]
            order_id = row.get("order_id", "")
            if not row.get(SPEC["key"]):
                counts["isolated"] += 1
                issues.append({"line": record["line"], "key": "", "code": "KEY_MISSING",
                               "order_id": order_id,
                               "order_ids": [order_id] if order_id else []})
            else:
                valid.append(row)
    grouped = OrderedDict()
    for row in valid:
        grouped.setdefault(row[SPEC["key"]], []).append(row)
    accepted = []
    for key, rows in grouped.items():
        signatures = {tuple(row[column] for column in SPEC["columns"]) for row in rows}
        order_ids = sorted({row.get("order_id", "") for row in rows if row.get("order_id")})
        if len(signatures) == 1:
            accepted.append(rows[0])
            counts["accepted"] += 1
            counts["duplicates"] += len(rows) - 1
            for row in rows[1:]:
                issues.append({"line": row["_line"], "key": key, "code": "EXACT_DUPLICATE",
                               "order_id": row.get("order_id", order_ids[0] if order_ids else ""),
                               "order_ids": order_ids})
        else:
            counts["isolated"] += len(rows)
            for row in rows:
                issues.append({"line": row["_line"], "key": key, "code": "CONFLICT_DUPLICATE",
                               "order_id": row.get("order_id", order_ids[0] if order_ids else ""),
                               # One product line claimed by two orders lands
                               # every related order in this attribution set.
                               "order_ids": order_ids})
    accepted.sort(key=lambda row: row["_line"])
    return {"rows": accepted, "issues": issues, "counts": counts, "records": parsed_records}


def load(profile: str, path: str | None = None):
    sample = _path(profile, path)
    if not sample.is_file():
        raise DomainIssue("SAMPLE_UNKNOWN")
    raw = sample.read_bytes()
    if len(raw) > MAX_FILE:
        raise DomainIssue("FILE_TOO_LARGE", 413)
    parsed = read_records(raw, SPEC["columns"], MAX_ROWS)
    result = adjudicate(parsed["records"])
    result.update({"profile": profile, "path": sample,
                   "sha256": __import__("hashlib").sha256(raw).hexdigest()})
    return result
