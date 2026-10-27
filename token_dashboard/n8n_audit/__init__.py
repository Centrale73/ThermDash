"""n8n-thermaudit: quality-adjusted thermal and energy efficiency audit of n8n workflows.

Merges ThermDash-AutoAudit's n8n graph scanner with ThermDash's E_in / W_useful / eta
engine and heat-dollar model. Entry points:

    python3 cli.py n8n-audit inspect WORKFLOW.json [--out draft.json] [--jurisdiction gdpr|law25]
    python3 cli.py n8n-audit run CONFIG.json [--out-dir DIR]

Method: docs/specs/n8n-thermaudit-method.md
"""
from .engine import energy_split, run  # noqa: F401
from .scan import scan  # noqa: F401
from .schedule import cron_runs_per_year, temporal_coverage  # noqa: F401
