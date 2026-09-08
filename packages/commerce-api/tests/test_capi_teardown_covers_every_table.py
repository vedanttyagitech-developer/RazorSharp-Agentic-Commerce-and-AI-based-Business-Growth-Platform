"""The teardown list must know about every table a test can write.

Three times in one day a new table was added, a test wrote to it, and the failure arrived as
a foreign-key violation while the tenant fixture was tearing down -- pointing at
a foreign-key violation on the merchants row, several frames from the test that actually
caused it, saying nothing about which table was new.

That is the expensive way to learn about an omission. This says it directly, before any test
runs, and names the table.

Ordering is not checked, only membership. Getting the order wrong also fails, but it fails
loudly at the right table and the message names both sides of the constraint; getting the
membership wrong is the one that reads as something else entirely.
"""

from __future__ import annotations

from typing import Final

from platform_db.schema import Base

from conftest import _TENANT_TABLES

#: Tables no tenant fixture deletes, and why each is exempt.
#:
#: ``tenants`` is deleted by its own statement after the loop, because it is the parent of
#: everything else. ``platform_operating_modes`` is in the list already despite sitting
#: outside row-level security.
EXEMPT: Final[frozenset[str]] = frozenset({"tenants"})


def test_every_tenant_owned_table_is_torn_down() -> None:
    owned = {
        name
        for name, table in Base.metadata.tables.items()
        if "tenant_id" in table.columns and name not in EXEMPT
    }
    missing = sorted(owned - set(_TENANT_TABLES))
    assert missing == [], (
        "these tables carry a tenant_id and are not in conftest._TENANT_TABLES, so a test "
        "that writes one fails while the tenant fixture tears down -- as a foreign-key "
        "violation on the merchants row, several frames away from the test that caused "
        f"it, naming neither: {missing}"
    )


def test_the_list_names_no_table_that_does_not_exist() -> None:
    """Drift the other way: an entry for a table that has been dropped.

    Harmless at run time -- the delete matches nothing -- but it makes the list look
    maintained when it is not, and the next reader trusts it.
    """
    unknown = sorted(set(_TENANT_TABLES) - set(Base.metadata.tables))
    assert unknown == [], (
        f"conftest._TENANT_TABLES names tables the schema does not have: {unknown}"
    )
