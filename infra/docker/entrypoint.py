"""Container entrypoint for the Python services (commerce-api, action-executor, migrations).

Two jobs, then exec:

1. Secret files -> environment. The GKE Secret Manager add-on mounts secrets as files and
   does not sync them into Kubernetes Secrets or environment variables (documented
   limitation of the add-on). The SecretProviderClass therefore names each file after the
   environment variable it carries (``path: DATABASE_URL_APP``) and this entrypoint
   exports it. A variable already present in the environment wins, so ``docker run -e``
   and local runs behave as expected. Values are never logged; names are.

2. ``${VAR}`` expansion of the command line, so the Dockerfile's exec-form CMD can refer to
   ``APP_MODULE`` / ``PORT`` without a shell. Kubernetes' own ``$(VAR)`` syntax is expanded
   by the kubelet before this runs; both work.

Finally ``os.execvp`` replaces this process with the service, so PID 1 is the service and
signals (SIGTERM from Kubernetes) reach it directly.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

DEFAULT_SECRETS_DIR = "/var/run/secrets/app"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_secret_files(directory: str) -> list[str]:
    root = Path(directory)
    if not root.is_dir():
        return []
    loaded: list[str] = []
    for entry in sorted(root.iterdir()):
        # The CSI driver and the Kubernetes atomic writer use `..data`/`..YYYY` symlink
        # directories for atomic updates; only the top-level file names are secrets.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        if not ENV_NAME.match(entry.name):
            print(f"entrypoint: ignoring {entry.name!r}: not an environment variable name", file=sys.stderr)
            continue
        if entry.name in os.environ:
            print(f"entrypoint: {entry.name} already set in the environment; file ignored", file=sys.stderr)
            continue
        # Strip only the trailing newline a hand-edited secret version tends to carry.
        # Anything else is the value.
        value = entry.read_bytes().decode("utf-8").rstrip("\r\n")
        if not value:
            print(f"entrypoint: {entry.name} is empty; not exported", file=sys.stderr)
            continue
        os.environ[entry.name] = value
        loaded.append(entry.name)
    return loaded


def main(argv: list[str]) -> int:
    if not argv:
        print("entrypoint: no command given (the image CMD or the Pod args are missing)", file=sys.stderr)
        return 2
    names = load_secret_files(os.environ.get("APP_SECRETS_DIR", DEFAULT_SECRETS_DIR))
    if names:
        print(f"entrypoint: loaded {len(names)} secret(s) from files: {', '.join(names)}", file=sys.stderr)
    command = [os.path.expandvars(arg) for arg in argv]
    try:
        os.execvp(command[0], command)
    except OSError as exc:  # pragma: no cover - only reachable with a broken image
        print(f"entrypoint: cannot exec {command[0]!r}: {exc.strerror}", file=sys.stderr)
        return 127
    return 0  # unreachable: execvp does not return on success


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
