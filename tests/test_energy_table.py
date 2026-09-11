"""Unit tests for energy constants loading and lookup."""

import pytest

from token_dashboard.energy_table import all_constants, lookup
from token_dashboard.thermodynamics import MissingEnergyConstantsError


def test_direct_lookup_gpt4o():
    c = lookup("gpt-4o")
    assert c.model == "gpt-4o"
    assert c.gpu == "H100"
    assert c.e_prefill == 0.00003
    assert c.e_decode == 0.00035
    assert c.measured is False
    assert "luccioni2024" in c.source


def test_direct_lookup_claude_sonnet():
    c = lookup("claude-3-5-sonnet")
    assert c.gpu == "A100"
    assert c.e_prefill == 0.000025
    assert c.e_decode == 0.000310
    assert c.measured is False


def test_snapshot_suffix_stripping():
    c = lookup("gpt-4o-2024-08-06")
    # Preserves requested model name or maps to base constants
    assert c.e_prefill == 0.00003
    assert c.e_decode == 0.00035
    assert c.gpu == "H100"


def test_snapshot_suffix_eight_digits():
    c = lookup("gpt-4o-20241022")
    assert c.e_prefill == 0.00003
    assert c.e_decode == 0.00035


def test_fuzzy_prefix_warns():
    with pytest.warns(UserWarning, match="Fuzzy energy constants fallback"):
        c = lookup("claude-3-5-sonnet-latest")
        assert c.e_decode == 0.000310


def test_missing_model_raises():
    with pytest.raises(MissingEnergyConstantsError, match="No energy constants found"):
        lookup("unknown-future-model-999")


def test_empty_model_raises():
    with pytest.raises(MissingEnergyConstantsError):
        lookup("")
    with pytest.raises(MissingEnergyConstantsError):
        lookup(None)  # type: ignore


def test_all_constants():
    all_c = all_constants()
    assert len(all_c) >= 6
    model_names = [c.model for c in all_c]
    assert "gpt-4o" in model_names
    assert "gemini-3.7-flash" in model_names
    for c in all_c:
        assert c.source != ""
        assert c.e_prefill > 0
        assert c.e_decode > 0
