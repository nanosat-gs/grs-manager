"""Porta de entrada do GRS Manager: contrato que o adapter rotctld espera
de quem estiver atrás dele — seja um caso de uso local, seja (como aqui) um
proxy que fala com o Station Manager via ZMQ.
"""

from __future__ import annotations

from typing import Protocol

from grs_manager.domain.models import RotorPosition


class RotorControlUseCase(Protocol):
    def set_target(self, azimuth_degrees: float, elevation_degrees: float) -> RotorPosition: ...
    def get_position(self) -> RotorPosition: ...
    def stop(self) -> None: ...
    def park(self) -> None: ...
