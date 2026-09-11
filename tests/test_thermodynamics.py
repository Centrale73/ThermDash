"""Unit tests for the pure thermodynamic efficiency computation module."""

import pytest

from token_dashboard.thermodynamics import (
    EfficiencyReport,
    EnergyConstants,
    MissingEnergyConstantsError,
    efficiency,
    energy_input,
    useful_work,
)


@pytest.fixture
def sample_constants() -> EnergyConstants:
    return EnergyConstants(
        model="gpt-4o",
        gpu="H100",
        e_prefill=0.00003,
        e_decode=0.00035,
        kappa=1.0,
        source="luccioni2024",
        measured=False,
    )


def test_energy_input_math(sample_constants: EnergyConstants):
    # E_in = 1.0 * (100 * 0.00003 + 50 * 0.00035) = 0.003 + 0.0175 = 0.0205 J
    e_in = energy_input(100, 50, sample_constants)
    assert pytest.approx(e_in, rel=1e-6) == 0.0205


def test_useful_work_math(sample_constants: EnergyConstants):
    # E_out = 1.0 * 50 * 0.00035 = 0.0175 J
    # Q = 0.8 -> W_useful = 0.8 * 0.0175 = 0.014 J
    w = useful_work(50, sample_constants, q=0.8)
    assert pytest.approx(w, rel=1e-6) == 0.014


def test_useful_work_clamping(sample_constants: EnergyConstants):
    # Q > 1 clamped to 1.0; Q < 0 clamped to 0.0
    w_high = useful_work(50, sample_constants, q=1.5)
    w_max = useful_work(50, sample_constants, q=1.0)
    assert pytest.approx(w_high) == w_max

    w_low = useful_work(50, sample_constants, q=-0.2)
    assert pytest.approx(w_low) == 0.0


def test_efficiency_q_none_defaults_to_unadjusted(sample_constants: EnergyConstants):
    report = efficiency(100, 50, sample_constants, q=None)
    assert report.quality_adjusted is False
    assert report.q is None
    # Q defaults to 1.0 in calculation
    assert pytest.approx(report.w_useful) == pytest.approx(report.e_out)
    assert report.eta == pytest.approx(report.w_useful / report.e_in)
    assert report.eta <= 1.0


def test_efficiency_q_specified(sample_constants: EnergyConstants):
    report_unadjusted = efficiency(100, 50, sample_constants, q=None)
    report_scored = efficiency(100, 50, sample_constants, q=0.3)

    assert report_scored.quality_adjusted is True
    assert report_scored.q == 0.3
    assert report_scored.w_useful < report_unadjusted.w_useful
    assert pytest.approx(report_scored.w_useful) == pytest.approx(0.3 * report_scored.e_out)
    assert report_scored.eta < report_unadjusted.eta


def test_waste_arithmetic_invariant(sample_constants: EnergyConstants):
    report = efficiency(500, 200, sample_constants, q=0.75)
    # Conservation of energy: E_in = W_useful + waste_joules
    assert pytest.approx(report.w_useful + report.waste_joules, rel=1e-6) == report.e_in
    assert pytest.approx(report.waste_share, rel=1e-6) == (1.0 - report.eta)
    assert 0.0 <= report.eta <= 1.0


def test_zero_tokens_edge_case(sample_constants: EnergyConstants):
    report = efficiency(0, 0, sample_constants, q=None)
    assert report.e_in == 0.0
    assert report.e_out == 0.0
    assert report.w_useful == 0.0
    assert report.eta == 0.0
    assert report.waste_joules == 0.0
    assert report.waste_share == 0.0


def test_negative_tokens_raise_error(sample_constants: EnergyConstants):
    with pytest.raises(ValueError):
        energy_input(-1, 10, sample_constants)

    with pytest.raises(ValueError):
        useful_work(-5, sample_constants, 0.5)

    with pytest.raises(ValueError):
        efficiency(10, -2, sample_constants)


def test_dataclass_immutability(sample_constants: EnergyConstants):
    with pytest.raises((AttributeError, TypeError)):
        sample_constants.e_prefill = 0.99  # type: ignore

    report = efficiency(10, 10, sample_constants)
    with pytest.raises((AttributeError, TypeError)):
        report.eta = 0.5  # type: ignore


def test_missing_constants_error_inheritance():
    assert issubclass(MissingEnergyConstantsError, ValueError)
