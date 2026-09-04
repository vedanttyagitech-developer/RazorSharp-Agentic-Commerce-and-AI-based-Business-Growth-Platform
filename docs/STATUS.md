# Evidence-driven status

Per specification section 35, no component is described as working without evidence.
`Verified` means a test in this repository proves it and CI runs that test.

| Component | Status | Evidence |
| --- | --- | --- |
| Money, integer minor units | **Verified** | `packages/commerce-domain/tests/test_money.py`, incl. Hypothesis allocation-conservation property |
| RFC 8785 JCS canonicalization | **Verified (integer profile)** | `test_jcs.py`, incl. UTF-16 key ordering and float rejection |
| Canonical checkout hash | **Verified** | `test_hashing.py`, frozen regression vector |
| UUIDv7 identifiers | **Verified** | `test_ids.py` |
| Transaction Assurance Kernel | Planned | — |
| Razorpay test-mode adapter | Planned | — |
| Reconciliation Service | Planned | — |
| Resolution Service | Planned | — |
| Human review queue | Planned | — |
| Commerce Assistant agents | Planned | — |
| Realtime STT/TTS | Planned | — |
| UCP 2026-08-25 | Planned | — |
| AP2 v0.2 human-present | Planned | Dependency resolution proven on Python 3.14 |
| ACP 2026-04-17 | Planned | — |
| MCP | Planned (P0 tail) | — |

## Deliberate profile restrictions

**JCS canonicalizes integers only.** RFC 8785's hardest requirement is ECMAScript number
serialization. This domain has no non-integer numbers: money is integer minor units,
quantities, versions and epoch timestamps are integers. A float reaching canonicalization
is a bug — most likely money that escaped the `Money` type — so it raises rather than
rounds. Enabling floats requires implementing ECMAScript number serialization and proving
it against the official RFC 8785 number vectors first.

## Verified environment facts

- Python **3.14.6**. The full dependency set resolves and imports: FastAPI, Pydantic,
  SQLAlchemy 2, psycopg 3, Alembic, httpx, `google-adk` 2.8.0, `google-genai` 2.22.0,
  `google-cloud-texttospeech` 2.37.0, `razorpay` 2.0.1.
- **AP2 pins `==` on three libraries** (`jwcrypto==1.5.6`, `cryptography==46.0.5`,
  `pydantic==2.12.5`). Resolution succeeds only when AP2's pins take precedence; the
  whole stack then coexists in one environment with all imports green. Do not raise those
  three libraries independently.
- AP2 publishes **no PyPI package**; it is pinned by git commit
  `b4587ac1d055888a73b4b21750973cffba961793`.
