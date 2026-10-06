"""Static checks on the Alembic revisions.

These exist because a migration that is merely *importable* can still crash the
deploy. Revision a7b8c9d0e1f2 shipped with a literal ":legacy" inside an
`op.execute()` string; `op.execute` puts a raw string through `sa.text()`,
which read that as a bind parameter and failed at run time with

    A value is required for bind parameter 'legacy'

On Railway the migration runs as part of the container's start command, so the
container exited before uvicorn and the whole service went down. Nothing in the
test suite touched it, because the suite deliberately runs without Postgres.

Compiling each statement catches that class of fault without needing a
database: compilation is where the bind parameters are resolved.
"""
import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
MIGRATIONS = sorted(VERSIONS.glob("*.py"))


class _FakeBind:
    """Enough of a Connection for `sa.Enum(...).create(op.get_bind())`."""

    def __init__(self, sink: list[object]) -> None:
        self.dialect = postgresql.dialect()
        self._sink = sink

    def execute(self, statement, *_a, **_k):
        self._sink.append(statement)
        return None

    def _run_ddl_visitor(self, *_a, **_k):
        return None


class _RecordingOp:
    """Stands in for `alembic.op`, capturing SQL and ignoring schema calls."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement, *_a, **_k) -> None:
        self.statements.append(statement)

    def get_bind(self):
        return _FakeBind(self.statements)

    def __getattr__(self, _name):
        # add_column, create_index, drop_index, ... are structural and carry no
        # bind parameters; accept and discard them.
        def _noop(*_a, **_k):
            return None

        return _noop


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids(paths):
    return [p.stem for p in paths]


def test_there_is_exactly_one_migration_head():
    """Two heads make `alembic upgrade head` ambiguous and fail the deploy."""
    revisions, downs = {}, set()
    for path in MIGRATIONS:
        module = _load(path)
        revisions[module.revision] = path.name
        if module.down_revision:
            downs.add(module.down_revision)
    heads = set(revisions) - downs
    assert len(heads) == 1, f"expected one head, found {[revisions[h] for h in heads]}"


@pytest.mark.parametrize("path", MIGRATIONS, ids=_ids(MIGRATIONS))
def test_migration_sql_compiles_with_every_parameter_bound(path):
    """Every statement a migration runs must compile on PostgreSQL.

    Compilation resolves bind parameters, so an accidental ":word" in a raw
    string — a Postgres cast, a literal containing a colon — is caught here
    rather than at 3am against the production database.
    """
    for direction in ("upgrade", "downgrade"):
        module = _load(path)
        recorder = _RecordingOp()
        module.op = recorder
        getattr(module, direction)()

        for statement in recorder.statements:
            clause = sa.text(statement) if isinstance(statement, str) else statement
            compiled = clause.compile(dialect=postgresql.dialect())
            # Raises InvalidRequestError for any parameter left without a value.
            compiled.construct_params()


def test_the_booking_intent_backfill_binds_its_legacy_suffix():
    """Pin the specific statement that took the service down."""
    module = _load(VERSIONS / "a7b8c9d0e1f2_booking_intent_key.py")
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()

    assert len(recorder.statements) == 1
    compiled = recorder.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.construct_params() == {"suffix": ":legacy"}
    assert "booking_intent_key" in str(compiled)
