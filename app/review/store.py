import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS order_issuances(
              run_id TEXT NOT NULL,
              order_id TEXT NOT NULL,
              amount INTEGER NOT NULL,
              amount_version INTEGER NOT NULL DEFAULT 1,
              voucher TEXT NOT NULL,
              idempotency_key TEXT,
              revision INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(run_id, order_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_issuance_idem
              ON order_issuances(run_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    def update_payload(self, run_id, payload, expected_revision=None, status=None, comment=None):
        """Replace the payload, optionally guarding the revision."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if expected_revision is not None and row["revision"] != expected_revision:
                return None, "stale"
            next_revision = row["revision"] + 1
            if status is None:
                db.execute("UPDATE runs SET payload=?, revision=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), next_revision, run_id))
            elif comment is None:
                db.execute("UPDATE runs SET payload=?, revision=?, status=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), next_revision, status, run_id))
            else:
                db.execute("UPDATE runs SET payload=?, revision=?, status=?, comment=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), next_revision, status, comment, run_id))
        return self.get(run_id), None

    # ------------------------------------------------------------------
    # Order issuance / vouchers
    # ------------------------------------------------------------------
    def get_issuance(self, run_id, order_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM order_issuances WHERE run_id=? AND order_id=?",
                             (run_id, order_id)).fetchone()
        return dict(row) if row else None

    def find_issuance_by_key(self, run_id, idempotency_key):
        if not idempotency_key:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM order_issuances WHERE run_id=? AND idempotency_key=?",
                             (run_id, idempotency_key)).fetchone()
        return dict(row) if row else None

    def list_issuances(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM order_issuances WHERE run_id=? ORDER BY order_id",
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def insert_issuance(self, run_id, order_id, amount, amount_version, voucher,
                        idempotency_key, payload, expected_revision=None):
        """Atomically issue an order.

        Returns ``(issuance_row, problem)`` where problem is one of
        ``None``, ``"missing"``, ``"stale"`` or ``"exists"``.  An existing
        issuance is returned as ``"exists"`` without mutating anything, which
        gives exactly-once semantics for retries.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            existing = db.execute("SELECT * FROM order_issuances WHERE run_id=? AND order_id=?",
                                  (run_id, order_id)).fetchone()
            if existing is not None:
                return dict(existing), "exists"
            if idempotency_key:
                replayed = db.execute(
                    "SELECT * FROM order_issuances WHERE run_id=? AND idempotency_key=?",
                    (run_id, idempotency_key)).fetchone()
                if replayed is not None:
                    return dict(replayed), "exists"
            if expected_revision is not None and row["revision"] != expected_revision:
                return None, "stale"
            next_revision = row["revision"] + 1
            voucher_text = json.dumps(voucher, ensure_ascii=False)
            db.execute(
                """INSERT INTO order_issuances
                   (run_id, order_id, amount, amount_version, voucher,
                    idempotency_key, revision, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, order_id, int(amount), int(amount_version or 1), voucher_text,
                 idempotency_key, next_revision, created_at))
            db.execute("UPDATE runs SET payload=?, revision=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), next_revision, run_id))
            inserted = db.execute("SELECT * FROM order_issuances WHERE run_id=? AND order_id=?",
                                  (run_id, order_id)).fetchone()
        return dict(inserted), None
