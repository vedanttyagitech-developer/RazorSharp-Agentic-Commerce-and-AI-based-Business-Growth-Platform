"""Test package marker for ``agent-runtime``.

Present so these modules import as ``tests.test_ar_*`` rather than as top-level modules
named by their basename. ADR 0003 D12 requires unique basenames across the repository
under pytest's prepend import mode; the ``test_ar_`` prefix satisfies that on its own, and
the package marker makes the guarantee structural rather than a naming convention someone
has to remember.
"""
