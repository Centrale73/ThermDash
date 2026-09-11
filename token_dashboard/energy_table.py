"""Loader and lookup module for LLM energy constants."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Union
import warnings

from .thermodynamics import EnergyConstants, MissingEnergyConstantsError

_DEFAULT_ENERGY_JSON = Path(__file__).parent.parent / "energy.json"
_CACHE: dict[str, tuple[float, dict]] = {}


def _get_raw_data(path: Path) -> dict:
    resolved = path.resolve()
    key = str(resolved)
    if not resolved.exists():
        raise FileNotFoundError(f"Energy constants file not found: {resolved}")
    mtime = resolved.stat().st_mtime
    cached = _CACHE.get(key)
    if cached is None or cached[0] != mtime:
        data = json.loads(resolved.read_text(encoding="utf-8"))
        _CACHE[key] = (mtime, data)
    return _CACHE[key][1]


def _strip_snapshot_suffix(model_id: str) -> str:
    # Matches suffixes like -2024-08-06, -20240806, -0613, -0125, -preview, -latest
    cleaned = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8}|\d{4})$", "", model_id)
    return cleaned


def lookup(
    model_id: str | None,
    path: Union[str, Path, None] = None,
) -> EnergyConstants:
    """Look up EnergyConstants for a model identifier.

    Matching precedence:
    1. Exact match
    2. Date/snapshot suffix stripped (e.g. gpt-4o-2024-08-06 -> gpt-4o)
    3. Longest prefix / fuzzy match with a warning
    4. Raises MissingEnergyConstantsError if no match found
    """
    if not model_id:
        raise MissingEnergyConstantsError("Model identifier is empty or None")

    json_path = Path(path) if path else _DEFAULT_ENERGY_JSON
    data = _get_raw_data(json_path)
    models = data.get("models", {})

    # 1. Exact match
    if model_id in models:
        m = models[model_id]
        return EnergyConstants(
            model=model_id,
            gpu=m["gpu"],
            e_prefill=float(m["e_prefill"]),
            e_decode=float(m["e_decode"]),
            kappa=float(m.get("kappa", 1.0)),
            source=str(m.get("source", "")),
            measured=bool(m.get("measured", False)),
        )

    # 2. Strip snapshot suffix
    stripped = _strip_snapshot_suffix(model_id)
    if stripped != model_id and stripped in models:
        m = models[stripped]
        return EnergyConstants(
            model=model_id,
            gpu=m["gpu"],
            e_prefill=float(m["e_prefill"]),
            e_decode=float(m["e_decode"]),
            kappa=float(m.get("kappa", 1.0)),
            source=str(m.get("source", "")),
            measured=bool(m.get("measured", False)),
        )

    # 3. Longest prefix match
    model_lower = model_id.lower()
    matched_key = None
    longest_len = 0
    for key in models:
        key_lower = key.lower()
        if model_lower.startswith(key_lower) or key_lower.startswith(model_lower):
            if len(key_lower) > longest_len:
                matched_key = key
                longest_len = len(key_lower)

    if matched_key is not None:
        warnings.warn(
            f"Fuzzy energy constants fallback: '{model_id}' matched '{matched_key}'",
            UserWarning,
            stacklevel=2,
        )
        m = models[matched_key]
        return EnergyConstants(
            model=model_id,
            gpu=m["gpu"],
            e_prefill=float(m["e_prefill"]),
            e_decode=float(m["e_decode"]),
            kappa=float(m.get("kappa", 1.0)),
            source=str(m.get("source", "")),
            measured=bool(m.get("measured", False)),
        )

    raise MissingEnergyConstantsError(
        f"No energy constants found for model '{model_id}'"
    )


def all_constants(path: Union[str, Path, None] = None) -> list[EnergyConstants]:
    """Return EnergyConstants for all models declared in the table."""
    json_path = Path(path) if path else _DEFAULT_ENERGY_JSON
    data = _get_raw_data(json_path)
    result = []
    for model_id, m in data.get("models", {}).items():
        result.append(
            EnergyConstants(
                model=model_id,
                gpu=m["gpu"],
                e_prefill=float(m["e_prefill"]),
                e_decode=float(m["e_decode"]),
                kappa=float(m.get("kappa", 1.0)),
                source=str(m.get("source", "")),
                measured=bool(m.get("measured", False)),
            )
        )
    return result
