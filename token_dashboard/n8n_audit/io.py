"""Command handlers shared by cli.py and the standalone skill wrapper."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import run
from .report import figures, report_md
from .scan import scan


def draft_config(wf_path: Path, a: dict, jurisdiction: str) -> dict:
    site = a["inference_sites"][0] if a["inference_sites"] else {}
    return {"workflow": str(wf_path), "jurisdiction": jurisdiction,
            "inference_node": site.get("node"), "model": site.get("model"),
            "runs_per_year": a["schedule"]["runs_per_year"],
            "items": {"mean": None, "cap": next((s["item_cap"] for s in a["input_shaping"]
                                                 if "item_cap" in s), None)},
            "item_samples_file": "TODO: JSON list of rendered per-item strings",
            "output_model": {"fixed_tokens": None, "per_item_tokens": None, "item_fraction_listed": None},
            "quality": None, "delivery": {"p_success": 1.0}, "energy_scenarios": None,
            "region": "global_baseline", "host": {"power_w": None, "workflows_sharing": 1},
            "monte_carlo": {"n": 20000, "seed": 7}}


def do_inspect(workflow: str, out: str | None, jurisdiction: str) -> dict:
    p = Path(workflow)
    a = scan(json.loads(p.read_text(encoding="utf-8")), jurisdiction)
    payload = {"scan": a, "draft_config": draft_config(p, a, jurisdiction)}
    if out:
        Path(out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def do_run(config: str, out_dir: str) -> dict:
    res = run(Path(config))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "report.md").write_text(report_md(res), encoding="utf-8")
    try:
        figures(res, out)
    except ImportError as e:  # matplotlib is optional (stdlib-only convention)
        print(f"[figures skipped: {e}]", file=sys.stderr)
    return res
