"""
output/__init__.py

Registry of output module types.

A new display — an HTTP wall, e-paper, a push notification — is a module
plus one line here.
"""

from __future__ import annotations

from output.base import BaseOutput
from output.console import ConsoleOutput

OUTPUTS: dict[str, type[BaseOutput]] = {
    "output_console": ConsoleOutput,
}
