"""Keeping every evaluation run, so quality can be seen over time.

One run tells you whether a version may ship. A row per run tells you
something a single run cannot: that accuracy has been drifting down for
a month, that a model swap three releases ago cost four questions, that
the last time a figure went unsupported was a Tuesday in March. A gate
answers "may this ship". History answers "is this getting worse", which
is the question you would rather ask early.

DuckDB because it is a file. There is no server to run, no migration to
apply and nothing to keep alive between releases, and the file can be
opened by anyone with a copy of it. The alternative — a table in the
service's own database — would tie the record of how well the thing
works to the thing working.

Failing to write history never fails a release. Losing a row is a
nuisance; blocking a good release because a log file was unwritable is a
worse outcome than the one being prevented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .evaluate import Report

DATABASE = Path("data/evaluations.duckdb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    ran_at              TIMESTAMP,
    model               VARCHAR,
    questions           INTEGER,
    correct             INTEGER,
    accuracy            DOUBLE,
    unsupported_figures INTEGER,
    withheld            INTEGER,
    threshold           DOUBLE,
    passed              BOOLEAN
)
"""


def record(report: Report, model: str, database: Path = DATABASE) -> bool:
    """Append one run. Returns whether it was written."""
    try:
        import duckdb

        database.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(database)) as connection:
            connection.execute(SCHEMA)
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    datetime.now(UTC),
                    model,
                    report.total,
                    report.correct,
                    round(report.accuracy, 2),
                    report.unsupported,
                    report.withheld,
                    report.threshold,
                    report.passed,
                ],
            )
        return True
    except Exception as problem:  # noqa: BLE001 — see the module docstring
        print(f"  (evaluation history not written: {problem})")
        return False


def history(database: Path = DATABASE, limit: int = 20) -> list[tuple]:
    """Recent runs, newest first."""
    if not database.exists():
        return []
    import duckdb

    with duckdb.connect(str(database), read_only=True) as connection:
        return connection.execute(
            "SELECT ran_at, model, accuracy, unsupported_figures, passed "
            "FROM runs ORDER BY ran_at DESC LIMIT ?",
            [limit],
        ).fetchall()
