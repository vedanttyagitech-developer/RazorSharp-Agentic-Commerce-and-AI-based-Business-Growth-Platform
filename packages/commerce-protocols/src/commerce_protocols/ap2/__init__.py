"""AP2 v0.2 human-present, specification 15.

Pinned to commit ``b4587ac1d055888a73b4b21750973cffba961793``, with the accepted
cryptographic profile narrowed by this project to ES256 on P-256. That narrowing is ours,
not a limitation of AP2, and specification 15.1 asks for it to be stated that way.

Importing this package silences the SDK's mandate logging as a side effect, which is
otherwise the first thing that would happen on any verification: ``ap2.sdk.mandate`` appends
the holder public key and the full presentation token to a file inside its own install
directory, and specification 15.5 is unambiguous that AP2 key material must never reach
logs. Doing it at import means no code path can reach a mandate operation before the patch
is in place.
"""

from __future__ import annotations

from .mandates import silence_sdk_disk_logging

silence_sdk_disk_logging()
