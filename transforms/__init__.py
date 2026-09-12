"""
transforms/__init__.py

Registry of transform types.

A new transform becomes available to every chain by adding one line to
TRANSFORMS — there is no other registration step and no core change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schemas.aircraft import Aircraft
from transforms.base import BaseTransform
from transforms.nop import Nop

if TYPE_CHECKING:
    from config import ChainConfig, Config

TRANSFORMS: dict[str, type[BaseTransform]] = {
    "nop": Nop,
}


def build_transforms(chain: "ChainConfig", app: "Config") -> list[BaseTransform]:
    """
    Instantiate a chain's transform list, in configured order.

    Names are validated at config load, so an unknown one here is a bug
    rather than user error — but fail loudly all the same.
    """
    built = []
    for name in chain.transform:
        cls = TRANSFORMS.get(name)
        if cls is None:
            raise KeyError(f"unknown transform '{name}' in chain '{chain.name}'")
        built.append(cls(name, chain, app))
    return built


def apply_transforms(
    transforms: list[BaseTransform], aircraft: list[Aircraft]
) -> list[Aircraft]:
    """Run a list of aircraft through every transform in order."""
    for transform in transforms:
        aircraft = transform.process(aircraft)
    return aircraft
