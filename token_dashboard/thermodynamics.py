"""Thermodynamic efficiency computation layer for LLM inference.

Computes E_in, E_out, W_useful, and quality-adjusted efficiency (eta).
Pure computation module: no I/O, fully typed, all energy units in Joules.
"""

from __future__ import annotations

from dataclasses import dataclass


class MissingEnergyConstantsError(ValueError):
    """Raised when no energy constants exist for a given model or alias."""


@dataclass(frozen=True)
class EnergyConstants:
    """Per-model-GPU energy constants for LLM inference."""

    model: str
    gpu: str
    e_prefill: float  # Joules per input token
    e_decode: float  # Joules per output token
    kappa: float = 1.0  # Model scaling factor (defaults to 1.0)
    source: str = ""  # Citation for measurement / interpolation
    measured: bool = False  # False if estimated/interpolated


@dataclass(frozen=True)
class EfficiencyReport:
    """Thermodynamic efficiency diagnostics for an LLM response."""

    e_in: float  # Joules — total energy consumed
    e_out: float  # Joules — decode-phase energy (kappa * n_out * e_decode)
    w_useful: float  # Joules — useful work (Q * e_out)
    q: float | None  # Quality gate score [0, 1], or None if unassessed
    quality_adjusted: bool  # True only if q was provided from real scoring
    eta: float  # Efficiency (w_useful / e_in), clamped to [0, 1]
    waste_joules: float  # Energy waste (e_in - w_useful)
    waste_share: float  # Waste share (1 - eta)
    constants: EnergyConstants


def energy_input(n_in: int, n_out: int, c: EnergyConstants) -> float:
    """Calculate total energy input E_in in Joules.

    E_in = kappa * (n_in * e_prefill + n_out * e_decode)
    """
    if n_in < 0 or n_out < 0:
        raise ValueError("Token counts must be non-negative")
    return float(c.kappa * (n_in * c.e_prefill + n_out * c.e_decode))


def useful_work(n_out: int, c: EnergyConstants, q: float) -> float:
    """Calculate useful work W_useful in Joules.

    E_out = kappa * n_out * e_decode
    W_useful = Q * E_out
    """
    if n_out < 0:
        raise ValueError("Token counts must be non-negative")
    q_clamped = max(0.0, min(1.0, float(q)))
    e_out = float(c.kappa * n_out * c.e_decode)
    return float(q_clamped * e_out)


def efficiency(
    n_in: int,
    n_out: int,
    c: EnergyConstants,
    q: float | None = None,
) -> EfficiencyReport:
    """Compute thermodynamic efficiency and waste diagnostics.

    When q is None: defaults Q = 1.0 and marks quality_adjusted = False.
    When n_in + n_out == 0 (zero tokens): returns zero-energy report with eta = 0.0.
    """
    if n_in < 0 or n_out < 0:
        raise ValueError("Token counts must be non-negative")

    quality_adjusted = q is not None
    q_eff = 1.0 if q is None else max(0.0, min(1.0, float(q)))

    e_in = energy_input(n_in, n_out, c)
    e_out = float(c.kappa * n_out * c.e_decode)
    w_useful = float(q_eff * e_out)

    if e_in <= 0.0:
        eta = 0.0
        waste_joules = 0.0
        waste_share = 0.0
    else:
        eta = max(0.0, min(1.0, w_useful / e_in))
        waste_joules = max(0.0, e_in - w_useful)
        waste_share = max(0.0, min(1.0, 1.0 - eta))

    return EfficiencyReport(
        e_in=e_in,
        e_out=e_out,
        w_useful=w_useful,
        q=q,
        quality_adjusted=quality_adjusted,
        eta=eta,
        waste_joules=waste_joules,
        waste_share=waste_share,
        constants=c,
    )
