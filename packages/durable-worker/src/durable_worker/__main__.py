"""Make ``python -m durable_worker`` mean what :mod:`durable_worker.main` says it means.

``main.py`` has documented ``python -m durable_worker`` as this process's invocation since
it was written, but a package only answers to ``-m`` if it holds a ``__main__``, and this
one did not. The command in the docstring therefore failed with ``No module named
durable_worker.__main__`` and the working form -- ``python -m durable_worker.main`` -- was
the one nobody had written down. That is a bad way to lose a worker: this is the only
process that spends an execution grant at Razorpay, so the recipe for starting it is
operational knowledge, and it lived in a shell history rather than in the tree.

The module keeps no logic of its own. It defers to :func:`durable_worker.main.main` so the
two entry paths cannot drift into two different startups -- argument parsing, logging
configuration and the redacting formatter all stay in one place, and ``--once`` behaves
identically whichever form is typed.

``scripts/run_demo.sh`` probes for this module first and falls back to ``main:main``, so it
worked either way; what changes here is that the documented command, the script's first
choice and the actual entry point are finally the same thing.
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
