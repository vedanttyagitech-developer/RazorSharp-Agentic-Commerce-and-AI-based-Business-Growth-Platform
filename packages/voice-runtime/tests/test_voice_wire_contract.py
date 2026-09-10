"""The current storefront must consume the exact generated server frame contract."""

import subprocess
import sys
from pathlib import Path

from voice_runtime.wire.frames import SessionReady

ROOT = Path(__file__).resolve().parents[3]
WIRE = ROOT / "apps/razorsharp-concept/lib/voice/wire.ts"


def test_current_frontend_contract_is_generated_without_drift():
    assert WIRE.exists(), "The real storefront voice contract must exist"
    subprocess.run(  # noqa: S603 -- fixed local generator and literal check argument
        [sys.executable, str(ROOT / "scripts/generate_voice_wire.py"), "--check"], check=True
    )


def test_voice_never_grants_payment_authority_and_version_is_explicit():
    ready = SessionReady(session_id="test")
    assert ready.voice_is_authority is False
    assert ready.protocol_version == 2
    assert '"voice_is_authority": false' in WIRE.read_text()


def test_client_imports_the_generated_contract():
    client = (WIRE.parent / "client.ts").read_text()
    assert "from './wire'" in client
