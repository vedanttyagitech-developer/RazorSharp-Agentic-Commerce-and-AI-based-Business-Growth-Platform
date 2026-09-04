"""Engine and session factories.

One engine per database role. A caller picks a role by asking for its sessionmaker, so
using the kernel's privileges is a deliberate act rather than an accident of import
order.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def database_url(role: str | None = None) -> str:
    """Resolve the connection URL, optionally for a specific role.

    Role credentials are separate environment entries so that the kernel's password is
    not readable by a process that only needs the app role.
    """
    if role:
        specific = os.environ.get(f"DATABASE_URL_{role.upper()}")
        if specific:
            return specific
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@lru_cache(maxsize=8)
def get_engine(role: str | None = None, *, echo: bool = False) -> Engine:
    return create_engine(
        database_url(role),
        echo=echo,
        pool_pre_ping=True,
        # Modest pool: reuse is exactly the condition the tenant-leak test targets.
        pool_size=5,
        max_overflow=5,
        future=True,
    )


@lru_cache(maxsize=8)
def get_sessionmaker(role: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(role), expire_on_commit=False, future=True)


@contextmanager
def session_scope(role: str | None = None) -> Iterator[Session]:
    """A transactional session that commits on success and rolls back on any exception."""
    factory = get_sessionmaker(role)
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
