"""core/env_compat.py — legacy environment aliases.

Phase 0 renamed every ``ODYSSEUS_*`` environment variable to
``MISANTROPIC_*``. Existing installs still carry the old names in ``.env``,
in systemd ``EnvironmentFile`` entries, in shell exports and in scheduled
task definitions, so a hard rename would silently drop that configuration
(e.g. a saved ``ODYSSEUS_ADMIN_PASSWORD``, or ``ODYSSEUS_SCRIPT_HOST``
driving scheduled scripts).

This module copies each legacy value onto its new name **only when the new
name is not already set**, so the modern spelling always wins and the
legacy spelling keeps working. Call it before anything reads config.

Drop this shim (and its three import sites) once the legacy names are no
longer in use.
"""

from __future__ import annotations

import logging
import os

_LEGACY_PREFIX = "ODYSSEUS_"
_NEW_PREFIX = "MISANTROPIC_"

logger = logging.getLogger(__name__)


def apply_legacy_env_aliases() -> list[str]:
    """Mirror ``ODYSSEUS_X`` onto ``MISANTROPIC_X`` where the latter is unset.

    Returns the list of new names that were populated, for logging/tests.
    """
    applied: list[str] = []
    for key, value in list(os.environ.items()):
        if not key.startswith(_LEGACY_PREFIX):
            continue
        new_key = _NEW_PREFIX + key[len(_LEGACY_PREFIX) :]
        if os.environ.get(new_key) is None:
            os.environ[new_key] = value
            applied.append(new_key)
    if applied:
        logger.info(
            "Using legacy ODYSSEUS_* environment variable(s) via alias: %s",
            ", ".join(sorted(applied)),
        )
    return applied


# Import-time application: covers `app:app`, the CLI entrypoints, and any
# module that just does `from core.database import ...`.
apply_legacy_env_aliases()
