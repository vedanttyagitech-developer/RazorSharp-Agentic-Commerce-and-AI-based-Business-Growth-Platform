"""The retired proxy must not reappear as a second browser authorization boundary.

Raw-token mint refusal, role isolation and mint responses are now exercised through
real backend sessions in test_capi_browser_mount.py. This source tripwire only keeps
an independent frontend route table from silently returning during a later rewrite.
"""

from pathlib import Path


def test_frontend_has_no_independent_api_proxy():
    root = Path(__file__).resolve().parents[3]
    apps = root / "apps"
    assert apps.is_dir(), "The frontend is required, not an optional skipped check"
    routes = list(apps.glob("*/app/api/**/route.ts"))
    assert not routes, f"Frontend API routes bypass the direct mount: {routes}"
    assert (apps / "razorsharp-concept/mounted/main.tsx").is_file()
