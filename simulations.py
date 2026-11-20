"""Firedrill-compatible simulation runner and hard-rule verifier for Parts Buyer.

Each drill runs inside an isolated temporary SQLite database so live demo data
is never modified. Evaluates all 4 hard rules (R3 approved suppliers, R5 live
recheck before order, R6 part spending cap enforcement, R7 zero duplicates)
across 7 stress-test scenarios.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import db
import rules

DEFAULT_PART_LIMITS = {"P01": 800.0, "P06": 1000.0}

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "stale_stock_p01",
        "name": "Stale stock catch (P01)",
        "rules": ["R5"],
        "description": "Broker A lists 8 in catalog but live recheck returns 0; must reject Broker A and order from OEM Direct ($725).",
    },
    {
        "id": "landed_cost_p02",
        "name": "Landed cost beats unit price (P02)",
        "rules": ["R4"],
        "description": "Broker B has lower unit price ($26 vs $28) but $60 international shipping; Broker A ($295) must beat Broker B ($320).",
    },
    {
        "id": "deadline_p04",
        "name": "Job deadline enforcement (P04)",
        "rules": ["R4"],
        "description": "Job J2 is due Nov 23 (must arrive by Nov 22); 10-day Broker B is ruled out and 2-day OEM Direct ($301) wins.",
    },
    {
        "id": "unapproved_and_cap_p06",
        "name": "Unapproved supplier block + cap hold (P06)",
        "rules": ["R3", "R6"],
        "description": "Broker B is unapproved for P06 (blocked); Broker A ($2,395) exceeds P06's $1,000 cap and waits for owner approval.",
    },
    {
        "id": "live_price_spike_p01",
        "name": "Live scan price jump crosses cap (P01)",
        "rules": ["R5", "R6"],
        "description": "Live supplier scan raises P01 from $175 to $195 ($805 total > $800 cap); buy price updates and P01 flips to approval.",
    },
    {
        "id": "idempotent_rerun",
        "name": "Same-day duplicate retry guard",
        "rules": ["R7"],
        "description": "Running twice on the same day must place zero duplicate orders and keep every daily order key unique.",
    },
    {
        "id": "crash_recovery",
        "name": "Crash after 2 orders + clean recovery",
        "rules": ["R7"],
        "description": "Forced crash after 2 orders leaves saved POs intact; rerun completes the remaining parts with zero duplicates.",
    },
]


@contextmanager
def _isolated_world(
    *,
    today: str = "2026-11-20",
    simulate_price_change: bool = False,
    crash_after: int = 0,
):
    """Run a simulation inside a throwaway SQLite DB and restore all env/globals afterward."""
    orig_db_path = db.DB_PATH
    env_keys = ("TODAY", "SIMULATE_PRICE_CHANGE", "CRASH_AFTER_ORDERS", "USE_COGNEE")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    with tempfile.TemporaryDirectory(prefix="firedrill_pb_") as tmpdir:
        db.DB_PATH = Path(tmpdir) / "sim_parts_buyer.db"
        os.environ["TODAY"] = today
        os.environ["SIMULATE_PRICE_CHANGE"] = "1" if simulate_price_change else "0"
        os.environ["CRASH_AFTER_ORDERS"] = str(crash_after)
        os.environ["USE_COGNEE"] = "0"
        try:
            db.reset_demo()
            yield db.DB_PATH
        finally:
            db.DB_PATH = orig_db_path
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _query(db_path: Path, sql: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql).fetchall()]


def _check_hard_rules(db_path: Path, part_limits: dict[str, float]) -> list[str]:
    """Verify zero hard-rule violations (R3, R5, R6, R7) against actual DB state."""
    violations: list[str] = []
    parts = {r["part_id"]: r for r in _query(db_path, "SELECT * FROM parts")}
    orders = _query(db_path, "SELECT * FROM purchase_orders ORDER BY po_id")
    logs = _query(db_path, "SELECT * FROM audit_log ORDER BY ts, id")

    # R7: unique idempotency keys
    keys = [o["idempotency_key"] for o in orders]
    if len(keys) != len(set(keys)):
        violations.append("R7: duplicate idempotency key found in purchase_orders")

    for po in orders:
        pid = po["part_id"]
        approved = set(parts[pid]["approved_suppliers"].split(";"))
        # R3: approved supplier
        if po["supplier_id"] not in approved:
            violations.append(f"R3: {po['po_id']} used unapproved supplier {po['supplier_id']} for {pid}")
        # R6: spending limit enforcement
        cap = part_limits.get(pid)
        if cap is not None and float(po["total"]) > cap and po["status"] == "placed" and not po["approved_by"]:
            violations.append(f"R6: {po['po_id']} placed ${po['total']} over {pid} cap ${cap} without approval")
        # R5: passing recheck recorded before order
        part_steps = [e for e in logs if e["part_id"] == pid]
        order_idx = next(
            (i for i, e in enumerate(part_steps) if e["step"] in ("order_placed", "approval_requested")),
            None,
        )
        has_recheck = order_idx is not None and any(
            e["step"] == "recheck" and json.loads(e["data_json"] or "{}").get("passed")
            for e in part_steps[:order_idx]
        )
        if not has_recheck:
            violations.append(f"R5: {po['po_id']} for {pid} lacked a passing live recheck before ordering")

    return violations


def run_drill(scenario_id: str, *, mode: str = "rules") -> dict[str, Any]:
    """Execute one named Firedrill scenario and return structured evidence + verdict."""
    meta = next((s for s in SCENARIOS if s["id"] == scenario_id), None)
    if meta is None:
        raise ValueError(f"Unknown scenario: {scenario_id}")

    t0 = time.perf_counter()
    part_limits = dict(DEFAULT_PART_LIMITS)
    checks: list[tuple[bool, str]] = []
    violations: list[str] = []

    def _run_once(crash_after: int = 0):
        os.environ["CRASH_AFTER_ORDERS"] = str(crash_after)
        if mode == "agent":
            import agent

            return agent.run_agent(part_limits=part_limits)
        return rules.run_rules_only(part_limits=part_limits)

    if scenario_id in ("stale_stock_p01", "landed_cost_p02", "deadline_p04", "unapproved_and_cap_p06"):
        with _isolated_world() as sim_db:
            _run_once()
            violations = _check_hard_rules(sim_db, part_limits)
            orders = {o["part_id"]: o for o in _query(sim_db, "SELECT * FROM purchase_orders")}
            logs = _query(sim_db, "SELECT * FROM audit_log ORDER BY ts, id")

            if scenario_id == "stale_stock_p01":
                p01_logs = [e for e in logs if e["part_id"] == "P01"]
                rechecked_brka = any(e["step"] == "recheck" and "BRKA" in (e["data_json"] or "") for e in p01_logs)
                po = orders.get("P01")
                checks.append((rechecked_brka, "Broker A live stock rechecked and failed (0 live vs 8 catalog)"))
                checks.append((bool(po and po["supplier_id"] == "OEMD" and abs(po["total"] - 725) < 0.01),
                               f"OEM Direct placed at $725 (got {po['supplier_id'] if po else 'none'} ${po['total'] if po else 0})"))

            elif scenario_id == "landed_cost_p02":
                po = orders.get("P02")
                checks.append((bool(po and po["supplier_id"] == "BRKA" and abs(po["total"] - 295) < 0.01),
                               f"Broker A won at $295 landed vs Broker B $320 (got {po['supplier_id'] if po else 'none'} ${po['total'] if po else 0})"))

            elif scenario_id == "deadline_p04":
                p04_rej = [e for e in logs if e["part_id"] == "P04" and e["step"] == "rejected_option" and "BRKB" in (e["data_json"] or "")]
                po = orders.get("P04")
                checks.append((bool(p04_rej), "Broker B (10d lead time) ruled out for Job J2 deadline"))
                checks.append((bool(po and po["supplier_id"] == "OEMD" and abs(po["total"] - 301) < 0.01),
                               f"OEM Direct placed at $301 (got {po['supplier_id'] if po else 'none'} ${po['total'] if po else 0})"))

            elif scenario_id == "unapproved_and_cap_p06":
                p06_rej = [e for e in logs if e["part_id"] == "P06" and e["step"] == "rejected_option" and "BRKB" in (e["data_json"] or "")]
                po = orders.get("P06")
                checks.append((bool(p06_rej), "Unapproved Broker B blocked on P06"))
                checks.append((bool(po and po["supplier_id"] == "BRKA" and po["status"] == "pending_approval" and abs(po["total"] - 2395) < 0.01),
                               f"Broker A ($2,395) held in pending_approval over $1,000 cap (got {po['status'] if po else 'none'})"))

    elif scenario_id == "live_price_spike_p01":
        with _isolated_world(simulate_price_change=True) as sim_db:
            _run_once()
            violations = _check_hard_rules(sim_db, part_limits)
            orders = {o["part_id"]: o for o in _query(sim_db, "SELECT * FROM purchase_orders")}
            parts = {r["part_id"]: r for r in _query(sim_db, "SELECT * FROM parts")}
            po = orders.get("P01")
            checks.append((abs(float(parts["P01"]["buy_price"]) - 195.0) < 0.01,
                           f"P01 live buy_price updated to $195 (got ${parts['P01']['buy_price']})"))
            checks.append((bool(po and po["status"] == "pending_approval" and abs(po["total"] - 805) < 0.01),
                           f"P01 ($805 > $800 cap) automatically flipped to pending_approval (got {po['status'] if po else 'none'} ${po['total'] if po else 0})"))

    elif scenario_id == "idempotent_rerun":
        with _isolated_world() as sim_db:
            _run_once()
            first_count = len(_query(sim_db, "SELECT * FROM purchase_orders"))
            _run_once()
            second_count = len(_query(sim_db, "SELECT * FROM purchase_orders"))
            violations = _check_hard_rules(sim_db, part_limits)
            checks.append((first_count == 4, f"Initial run recorded 4 orders/approvals (got {first_count})"))
            checks.append((second_count == first_count, f"Same-day rerun added {second_count - first_count} duplicate orders (want 0)"))

    elif scenario_id == "crash_recovery":
        with _isolated_world() as sim_db:
            crashed = False
            try:
                _run_once(crash_after=2)
            except RuntimeError as exc:
                crashed = "simulated crash" in str(exc).lower()
            mid_placed = sorted(
                o["part_id"] for o in _query(sim_db, "SELECT * FROM purchase_orders") if o["status"] == "placed"
            )
            _run_once(crash_after=0)
            final_orders = _query(sim_db, "SELECT * FROM purchase_orders")
            violations = _check_hard_rules(sim_db, part_limits)
            checks.append((crashed and mid_placed == ["P01", "P02"],
                           f"Stopped after 2 orders (crashed={crashed}, placed={mid_placed})"))
            checks.append((len(final_orders) == 4,
                           f"Recovery completed remaining parts with 0 duplicates ({len(final_orders)} total POs)"))

    passed = all(ok for ok, _ in checks) and not violations
    return {
        "id": meta["id"],
        "name": meta["name"],
        "rules": meta["rules"],
        "description": meta["description"],
        "passed": passed,
        "hard_rule_violations": violations,
        "checks": [{"pass": ok, "detail": detail} for ok, detail in checks],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def run_all_drills(*, mode: str = "rules") -> dict[str, Any]:
    """Run all Firedrill scenarios and compute the overall scorecard."""
    t0 = time.perf_counter()
    results = [run_drill(s["id"], mode=mode) for s in SCENARIOS]
    passed_count = sum(1 for r in results if r["passed"])
    total_violations = sum(len(r["hard_rule_violations"]) for r in results)
    return {
        "mode": mode,
        "total_scenarios": len(results),
        "passed_scenarios": passed_count,
        "pass_rate_pct": round(100.0 * passed_count / max(len(results), 1), 1),
        "hard_rule_violations": total_violations,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "results": results,
    }
