"""Schema: web-native media organizer and renamer."""

import os
import re

__version__ = "0.1.0"


def build_commit() -> str | None:
    """The git commit this build was made from (``SCHEMA_COMMIT``, set by the Docker build in CI),
    or None for a local / development run."""
    value = os.environ.get("SCHEMA_COMMIT", "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else None
