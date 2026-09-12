"""
ingest/__init__.py

Registry of ingest module types.

Adding a source means writing the module and adding one line here.
"""

from __future__ import annotations

from ingest.base import BaseIngest
from ingest.concorde.ingestor import ConcordeIngest

INGESTORS: dict[str, type[BaseIngest]] = {
    "ingest_concorde": ConcordeIngest,
}
