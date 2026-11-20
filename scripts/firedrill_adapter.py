#!/usr/bin/env python3
"""Firedrill target adapter for Parts Buyer.

Supports both:
- Firedrill stdin JSON invocation envelope (`{"scenario": "...", "mode": "rules"|"agent"}`)
- Direct CLI flags (`--all`, `--scenario <id>`, `--mode rules|agent`)

Always writes structured JSON to stdout (keeping logs off stdout per Firedrill spec).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import simulations


def main() -> int:
    parser = argparse.ArgumentParser(description="Firedrill simulation adapter for Parts Buyer")
    parser.add_argument("--scenario", help="Run a single scenario ID")
    parser.add_argument("--all", action="store_true", help="Run all scenarios")
    parser.add_argument("--mode", default="rules", choices=("rules", "agent"))
    args = parser.parse_args()

    payload = {}
    if not args.scenario and not args.all and not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}

    scenario = args.scenario or payload.get("scenario") or payload.get("scenario_id")
    mode = payload.get("mode", args.mode)

    if scenario:
        out = simulations.run_drill(scenario, mode=mode)
        ok = bool(out["passed"])
    else:
        out = simulations.run_all_drills(mode=mode)
        ok = out["passed_scenarios"] == out["total_scenarios"] and out["hard_rule_violations"] == 0

    print(json.dumps(out, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
