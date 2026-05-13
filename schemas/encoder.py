"""
schemas/encoder.py

JSON encoder for Squawk schema objects.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime


class SquawkEncoder(json.JSONEncoder):
    """
    Handles:
        - dataclasses    → dict
        - datetime       → ISO 8601 string
    """

    def default(self, obj: object) -> object:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)
