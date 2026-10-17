"""Disposable-PostgreSQL rehearsal; does not read CARA settings or .env files."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import uuid

BASE = "a105_square_catalog"
TARGET = "a106_provider_approval"
STATUS = "awaiting_provider_approval"


def worker():
    # The real migration environment imports this module. Supply only the one
    # attribute it uses, rather than importing application settings/.env.
    config_module = types.ModuleType("app.core.config")
    # Escape only the Alembic configparser boundary; driver URLs stay unchanged.
    config_module.settings = types.SimpleNamespace(
        DATABASE_URL_ASYNC=os.environ["REHEARSAL_DATABASE_URL"].replace("%", "%%")
    )
    sys.modules["app.core.config"] = config_module
    sys.path.insert(0, os.getcwd())
    from alembic.config import Config
    from alembic import command
    getattr(command, sys.argv[2])(Config("alembic.ini"), sys.argv[3])


async def main(args):
    import asyncpg
    from sqlalchemy.engine import make_url

    backend = Path(args.backend).resolve(strict=True)
    if not (backend / "migrations/versions/a106_provider_approval.py").is_file():
        raise RuntimeError("Backend does not contain the requested migration")
    url = make_url(args.test_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("PostgreSQL is required")
    if not (url.database or "").startswith("brick1_rehearsal_"):
        raise RuntimeError("Database name must start with brick1_rehearsal_")
    host = url.query.get("host", url.host or "")
    if not (host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")):
        raise RuntimeError("Only loopback or a local Unix socket is permitted")
    if not args.disposable_database:
        raise RuntimeError("Explicit --disposable-database acknowledgement required")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    async_url = url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    # Do not forward developer/provider credentials into Alembic subprocesses.
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
    env.update(REHEARSAL_DATABASE_URL=async_url, APP_ENV="local",
               SECRET_KEY="disposable-migration-rehearsal-only",
               TWILIO_ACCOUNT_SID="AC" + "0" * 32,
               TWILIO_AUTH_TOKEN="disposable", PYTHONNOUSERSITE="1")

    def migrate(direction, revision):
        subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        "--worker", direction, revision], cwd=backend,
                       env=env, check=True)

    async def connect():
        return await asyncpg.connect(dsn, timeout=10)

    async def revision(expected):
        conn = await connect()
        try:
            rows = await conn.fetch("SELECT version_num FROM alembic_version")
            assert [row[0] for row in rows] == [expected], "Unexpected migration revision"
        finally:
            await conn.close()

    conn = await connect()
    try:
        # Refuse any user tables/views/sequences/types, including other schemas.
        occupied = await conn.fetchval("""
            SELECT EXISTS (
              SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
            ) OR EXISTS (
              SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
              WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
            )
        """)
        if occupied:
            raise RuntimeError("Refusing a nonempty database")
    finally:
        await conn.close()
    print("PASS: empty disposable database preflight", flush=True)
    migrate("upgrade", BASE)
    await revision(BASE)
    tenant, contact, appointment = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = await connect()
    try:
        async with conn.transaction():
            await conn.execute("INSERT INTO spa_accounts (id,name,booking_provider) VALUES ($1,$2,'square')", tenant, "Disposable approval rehearsal")
            await conn.execute("INSERT INTO contacts (id,tenant_id,first_name,phone_number) VALUES ($1,$2,$3,$4)", contact, tenant, "Test Caller", "+12025550199")
            await conn.execute("""INSERT INTO appointments
                (id,tenant_id,contact_id,title,start_time,end_time,status,booking_provider,external_booking_id,booking_intent_key)
                VALUES ($1,$2,$3,'Disposable request','2035-01-15 15:00:00+00','2035-01-15 16:00:00+00','scheduled','square','fake-square-approval','rehearsal:approval')""", appointment, tenant, contact)
        try:
            async with conn.transaction():
                await conn.execute("UPDATE appointments SET status=$1::appointment_status WHERE id=$2", STATUS, appointment)
        except asyncpg.InvalidTextRepresentationError:
            print("PASS: production migration head rejects new status", flush=True)
        else:
            raise AssertionError("Production head unexpectedly accepted new status")
    finally:
        await conn.close()
    migrate("upgrade", TARGET)
    await revision(TARGET)
    conn = await connect()
    try:
        await conn.execute("UPDATE appointments SET status=$1::appointment_status WHERE id=$2", STATUS, appointment)
        assert await conn.fetchval("SELECT status::text FROM appointments WHERE id=$1", appointment) == STATUS
    finally:
        await conn.close()
    print("PASS: additive upgrade stores awaiting approval", flush=True)
    migrate("downgrade", BASE)
    await revision(BASE)
    conn = await connect()
    try:
        assert await conn.fetchval("SELECT status::text FROM appointments WHERE id=$1", appointment) == "scheduled"
        assert await conn.fetchval("SELECT count(*) FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='appointment_status' AND e.enumlabel=$1", STATUS) == 1
        assert await conn.fetchval("SELECT count(*) FROM appointments") == 1
    finally:
        await conn.close()
    print("PASS: rollback normalizes row and retains enum label", flush=True)
    migrate("upgrade", TARGET)
    await revision(TARGET)
    conn = await connect()
    try:
        await conn.execute("UPDATE appointments SET status=$1::appointment_status WHERE id=$2", STATUS, appointment)
        assert await conn.fetchval("SELECT status::text FROM appointments WHERE id=$1", appointment) == STATUS
    finally:
        await conn.close()
    print(json.dumps({"result": "PASS", "base": BASE, "head": TARGET,
                      "fixtures": "synthetic only", "database_left_intact": True}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--backend", required=True)
        parser.add_argument("--test-url", required=True)
        parser.add_argument("--disposable-database", action="store_true")
        args = parser.parse_args()
        try:
            asyncio.run(main(args))
        except Exception as exc:
            # Avoid printing database credentials in driver/subprocess errors.
            print("REHEARSAL FAILED: " + type(exc).__name__, file=sys.stderr)
            sys.exit(1)
