"""Content fingerprints for a retained source file.

The transcript cache is keyed by the SHA-256 of the source bytes. Hashing a
several-hundred-megabyte video on every analysis run is measurable work, so the
hash is computed once while the file is being persisted and then reused. The
value itself is unchanged, which keeps every existing cached transcript valid.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
