from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParetoDecision:
    delta_evo: float
    delta_reg: float
    accepted: bool


def decide(delta_evo: float, delta_reg: float) -> ParetoDecision:
    accepted = delta_evo >= 0 and delta_reg >= 0 and max(delta_evo, delta_reg) > 0
    return ParetoDecision(delta_evo=delta_evo, delta_reg=delta_reg, accepted=accepted)
