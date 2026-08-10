"""Value objects do GRS Manager."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RotorPosition:
    azimuth_degrees: float
    elevation_degrees: float
