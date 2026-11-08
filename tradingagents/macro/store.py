"""Append-only evidence and decisions; replaceable HTTP cache is separate."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import Observation


class MacroStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        repo = Path(__file__).resolve().parents[2]
        if self.path.is_relative_to(repo) or any((p / ".git").exists() for p in self.path.parents):
            raise ValueError("Macro SQLite must be outside Git repositories")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, analysis_date TEXT NOT NULL, evaluated_at TEXT NOT NULL,
                    ticker TEXT NOT NULL, configuration TEXT NOT NULL, report TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, comparison_key TEXT NOT NULL, series_key TEXT NOT NULL,
                    period TEXT NOT NULL, retrieved_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observations_key ON observations(comparison_key);
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    comparison_key TEXT NOT NULL, series_key TEXT NOT NULL, period TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL, analysis_date TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS decisions_key ON decisions(comparison_key, id);
                CREATE TABLE IF NOT EXISTS run_observations (
                    run_id TEXT NOT NULL REFERENCES runs(id), observation_id TEXT NOT NULL REFERENCES observations(id),
                    PRIMARY KEY(run_id, observation_id));
                CREATE TABLE IF NOT EXISTS http_cache (
                    key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT fetched_at,payload FROM http_cache WHERE key=?", (key,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def cache_put(self, key, at, payload):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?)", (key, at, json.dumps(payload))
            )

    def previous(self, o, as_of, cutoff):
        # Never borrow a decision evaluated after a historical cutoff, or one
        # concerning a later observation period. Same-period re-evaluations
        # use the preceding decision first, then earlier periods.
        with self.connect() as db:
            rows = db.execute(
                """SELECT payload FROM decisions WHERE series_key=? AND period<=?
                AND analysis_date<=? AND evaluated_at<=? ORDER BY period DESC,id DESC""",
                (o.series_key, o.period, as_of, cutoff),
            ).fetchall()
        for row in rows:
            data = json.loads(row[0])
            if data.get("selected"):
                return Observation.from_dict(data["selected"])
        return None

    def prior(self, key, as_of, cutoff):
        with self.connect() as db:
            row = db.execute(
                """SELECT payload FROM decisions WHERE comparison_key=? AND analysis_date<=?
                AND evaluated_at<=? ORDER BY id DESC LIMIT 1""",
                (key, as_of, cutoff),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, as_of, cutoff):
        """Read the prior decision/vintage index once per collection.

        Thousands of daily curve points must not reopen SQLite twice for
        every point. Include the last non-null selection when the newest
        assessment has no usable data, without changing that assessment.
        """
        with self.connect() as db:
            rows = db.execute(
                """SELECT payload FROM decisions WHERE id IN (
                SELECT MAX(id) FROM decisions WHERE analysis_date<=? AND evaluated_at<=?
                GROUP BY comparison_key
                UNION
                SELECT MAX(id) FROM decisions WHERE analysis_date<=? AND evaluated_at<=?
                AND json_extract(payload,'$.selected') IS NOT NULL GROUP BY comparison_key
                ) ORDER BY id DESC""",
                (as_of, cutoff, as_of, cutoff),
            ).fetchall()
        prior, accepted = {}, {}
        for (payload,) in rows:
            d = json.loads(payload)
            prior.setdefault(d["key"], d)
            if d.get("selected"):
                accepted.setdefault(d["key"], Observation.from_dict(d["selected"]))
        series = {}
        for o in accepted.values():
            series.setdefault(o.series_key, []).append(o)
        for values in series.values():
            values.sort(key=lambda o: o.period, reverse=True)
        return prior, series

    def open_conflicts(self, as_of, cutoff):
        with self.connect() as db:
            rows = db.execute(
                """SELECT payload FROM decisions WHERE id IN
                (SELECT MAX(id) FROM decisions WHERE analysis_date<=? AND evaluated_at<=?
                 GROUP BY comparison_key)""",
                (as_of, cutoff),
            ).fetchall()
        return [
            d
            for (s,) in rows
            if (d := json.loads(s)).get("conflict")
            or d.get("transition") == "UNRESOLVED_MISSING_DATA"
        ]

    def captured(self, as_of):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM observations WHERE retrieved_at<=?",
                (as_of + "T23:59:59.999999+00:00",),
            ).fetchall()
        return [Observation.from_dict(json.loads(row[0])) for row in rows]

    def save(self, snapshot, decisions, observations):
        """One transaction makes each immutable run independently replayable."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot["run_id"],
                    snapshot["as_of"],
                    snapshot["evaluated_at"],
                    snapshot["ticker"],
                    json.dumps(snapshot["configuration"], sort_keys=True),
                    snapshot["report"],
                    json.dumps(snapshot),
                ),
            )
            for o in observations:
                payload = json.dumps(o.to_dict(), sort_keys=True)
                identifier = hashlib.sha256(payload.encode()).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                    (identifier, o.key, o.series_key, o.period, o.retrieved_at, payload),
                )
                db.execute(
                    "INSERT OR IGNORE INTO run_observations VALUES (?,?)",
                    (snapshot["run_id"], identifier),
                )
            for d in decisions:
                db.execute(
                    """INSERT INTO decisions
                    (run_id,comparison_key,series_key,period,evaluated_at,analysis_date,payload)
                    VALUES (?,?,?,?,?,?,?)""",
                    (
                        snapshot["run_id"],
                        d.key,
                        d.series_key,
                        d.period,
                        snapshot["evaluated_at"],
                        snapshot["as_of"],
                        json.dumps(d.to_dict()),
                    ),
                )

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None
