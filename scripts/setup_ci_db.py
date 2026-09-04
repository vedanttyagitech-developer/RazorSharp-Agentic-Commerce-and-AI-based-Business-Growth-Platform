#!/usr/bin/env python3
"""Database setup and verification helper for CI and local development.

Actions:
1. Verifies connectivity to the target disposable test database.
2. Applies Alembic migrations up to head.
3. Bootstraps restricted test roles from scripts/bootstrap_test_roles.sql.
4. Verifies role security attributes (NOSUPERUSER, NOBYPASSRLS, LOGIN).
5. Verifies that restricted roles can establish connections.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "packages" / "platform-db" / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform-db" / "migrations"
BOOTSTRAP_SQL = REPO_ROOT / "scripts" / "bootstrap_test_roles.sql"

REQUIRED_RESTRICTED_ROLES = (
    "commerce_test_app",
    "commerce_test_kernel",
    "commerce_test_worker",
)


def _to_psycopg_conninfo(url: str) -> str:
    """Strip dialect driver like +psycopg from URL for psycopg.connect."""
    return re.sub(r"^postgresql\+[a-zA-Z0-9]+://", "postgresql://", url)


def _sanitize_url(raw_url: str) -> str:
    """Mask credentials in database URL for safe logging."""
    try:
        parsed = urlparse(raw_url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"{parsed.scheme}://***:***@{netloc}{parsed.path}"
    except Exception:
        return "<sanitized-url>"


def get_admin_url() -> str:
    url = os.environ.get("DATABASE_URL_TEST_ADMIN") or os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL_TEST_ADMIN or DATABASE_URL must be set.", file=sys.stderr)
        sys.exit(1)
    return url


def run_migrations(admin_url: str) -> None:
    print(f"Applying Alembic migrations to {_sanitize_url(admin_url)}...")
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", admin_url)
    command.upgrade(cfg, "head")
    print("Migrations successfully applied to head.")


def bootstrap_roles(admin_url: str) -> None:
    print(f"Bootstrapping test roles from {BOOTSTRAP_SQL.name}...")
    if not BOOTSTRAP_SQL.exists():
        print(f"ERROR: Bootstrap file not found at {BOOTSTRAP_SQL}", file=sys.stderr)
        sys.exit(1)

    sql = BOOTSTRAP_SQL.read_text(encoding="utf-8")
    with psycopg.connect(_to_psycopg_conninfo(admin_url), autocommit=True) as conn:
        conn.execute(sql)
    print("Test roles bootstrapped successfully.")


def verify_roles(admin_url: str) -> None:
    print("Verifying restricted test roles in pg_roles...")
    conninfo = _to_psycopg_conninfo(admin_url)
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
            FROM pg_roles
            WHERE rolname = ANY(%s)
            """,
            (list(REQUIRED_RESTRICTED_ROLES),),
        )
        rows = cur.fetchall()

    found_roles = {r[0]: {"super": r[1], "bypassrls": r[2], "canlogin": r[3]} for r in rows}

    missing = set(REQUIRED_RESTRICTED_ROLES) - set(found_roles.keys())
    if missing:
        print(f"ERROR: Missing required roles: {missing}", file=sys.stderr)
        sys.exit(1)

    for role_name, props in found_roles.items():
        s = props["super"]
        b = props["bypassrls"]
        c = props["canlogin"]
        print(f"  Role '{role_name}': super={s}, bypassrls={b}, canlogin={c}")
        if props["super"]:
            print(f"ERROR: {role_name} is SUPERUSER!", file=sys.stderr)
            sys.exit(1)
        if props["bypassrls"]:
            print(f"ERROR: {role_name} has BYPASSRLS!", file=sys.stderr)
            sys.exit(1)
        if not props["canlogin"]:
            print(f"ERROR: {role_name} cannot LOGIN!", file=sys.stderr)
            sys.exit(1)

    print("All restricted test roles verified (NOSUPERUSER, NOBYPASSRLS, LOGIN).")


def test_role_connections(admin_url: str) -> None:
    parsed = urlparse(_to_psycopg_conninfo(admin_url))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/") or "commerce_test"
    print("Testing connection establishment for each restricted role...")
    for role_name in REQUIRED_RESTRICTED_ROLES:
        role_url = f"postgresql://{role_name}:testpw@{host}:{port}/{dbname}"
        with psycopg.connect(role_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, session_user;")
            row = cur.fetchone()
            assert row is not None
            cur_user, _sess_user = row
            assert cur_user == role_name, f"Expected user {role_name}, got {cur_user}"
        print(f"  Role '{role_name}' connected successfully (current_user={cur_user}).")
    print("All restricted roles connected successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup and verify CI database.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify existing roles and migrations without making changes.",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Skip running migrations.",
    )
    args = parser.parse_args()

    admin_url = get_admin_url()

    if args.verify_only:
        verify_roles(admin_url)
        test_role_connections(admin_url)
        return

    if not args.skip_migrations:
        run_migrations(admin_url)

    bootstrap_roles(admin_url)
    verify_roles(admin_url)
    test_role_connections(admin_url)


if __name__ == "__main__":
    main()
