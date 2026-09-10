"""Run each package in its own pytest process so local conftest imports stay local."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    results = root / "test-results"
    results.mkdir(exist_ok=True)
    failed = []
    for tests in sorted((root / "packages").glob("*/tests")):
        package = tests.parent.name
        print(f"\nTesting {package}", flush=True)
        command = [
            sys.executable,
            "-m",
            "pytest",
            str(tests),
            f"--junitxml={results / (package + '.xml')}",
            *sys.argv[1:],
        ]
        # Fixed Python executable and module; remaining arguments come from the local CLI.
        if subprocess.run(command, cwd=root, check=False).returncode:  # noqa: S603
            failed.append(package)
    if failed:
        print(f"Failed packages: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
