"""The five agent tools wrapping rules.py and audit.py plus OpenAI tool-calling schemas.

All money, compliance, recheck, and idempotency rules live in rules.py.
This module only exposes thin wrappers, argument normalization, live event hooks,
and schemas for agent.py.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

import audit
import rules

EventCallback = Callable[[dict[str, Any]], None]

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_inventory_status",
            "description": (
                "Return every part with on_hand, reserved, on_order, available, reorder_point, "
                "status (OK / Short / Ordered / Awaiting approval), and today's order if any."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_part",
            "description": (
                "Evaluate one part against inventory rules, approved suppliers for that part, "
                "open repair job deadlines, and landed cost. Returns qty, deadline, eligible_offers "
                "(ranked lowest landed cost first), rejected offers with reasons, and next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part_id": {
                        "type": "string",
                        "description": "Part ID such as P01, P02, ..., P08",
                    }
                },
                "required": ["part_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recheck_offer",
            "description": (
                "Confirm live stock and current price with the supplier immediately before placing an order. "
                "Always call this before place_order, passing the offer's qty from eligible_offers (which includes any minimum order batch)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offer_id": {
                        "type": "string",
                        "description": "Offer ID such as O1, O2, ..., O11",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "Order quantity for that offer from eligible_offers (includes any minimum batch)",
                    },
                },
                "required": ["offer_id", "qty"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": (
                "Submit an order for a part using an offer that passed recheck_offer, passing that offer's qty. "
                "ALWAYS call this even if landed cost exceeds a part's spending limit: "
                "rules.py enforces approved suppliers, deadline, minimum batch quantity, live recheck, "
                "idempotency, and routes orders over the part's limit into pending_approval for the owner."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part_id": {"type": "string"},
                    "offer_id": {"type": "string"},
                    "qty": {"type": "integer"},
                },
                "required": ["part_id", "offer_id", "qty"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_reasoning",
            "description": (
                "Save a two or three sentence plain-English explanation for the shop owner as an agent_note "
                "on the part's audit trail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["part_id", "text"],
            },
        },
    },
]


def _crash_after() -> int:
    try:
        return int(os.getenv("CRASH_AFTER_ORDERS", "0") or 0)
    except ValueError:
        return 0


def _normalize_part_id(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    m = re.match(r"^(P\d+)", text)
    return m.group(1) if m else text


def _normalize_offer_id(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    m = re.match(r"^(O\d+)", text)
    return m.group(1) if m else text


class ToolSession:
    """Binds one agent run's run_id, spending limits, crash switch, and optional event hook."""

    def __init__(
        self,
        *,
        run_id: str,
        spend_limit: float | None = None,
        part_limits: dict[str, float] | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.run_id = run_id
        self.spend_limit = None if spend_limit is None else float(spend_limit)
        self.part_limits = part_limits
        self.crash_after = _crash_after()
        self.orders_placed_this_run = 0
        self.on_event = on_event
        self._offer_to_part: dict[str, str] = {}

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    def get_inventory_status(self) -> list[dict[str, Any]]:
        rows = rules.get_inventory_status()
        short_count = sum(1 for r in rows if r.get("status") == "Short")
        self._emit(
            {
                "type": "inventory_checked",
                "total_parts": len(rows),
                "short_count": short_count,
                "parts": rows,
            }
        )
        return rows

    def evaluate_part(self, part_id: str) -> dict[str, Any]:
        pid = _normalize_part_id(part_id)
        self._emit({"type": "part_start", "part_id": pid})
        ev = rules.evaluate_part(
            pid,
            self.run_id,
            spend_limit=self.spend_limit,
            part_limits=self.part_limits,
        )
        for o in ev.get("eligible_offers", []):
            if o.get("offer_id"):
                self._offer_to_part[o["offer_id"]] = pid
        for r in ev.get("rejected", []):
            if r.get("offer_id"):
                self._offer_to_part[r["offer_id"]] = pid
        self._emit(
            {
                "type": "part_evaluated",
                "part_id": pid,
                "name": ev.get("name", pid),
                "available": ev.get("available"),
                "reorder_point": ev.get("reorder_point"),
                "needs_buying": ev.get("needs_buying", False),
                "existing_order": ev.get("existing_order"),
                "qty": ev.get("qty", 0),
                "deadline": ev.get("deadline"),
                "spend_limit": ev.get("spend_limit"),
                "eligible_offers": ev.get("eligible_offers", []),
                "rejected": ev.get("rejected", []),
                "action": ev.get("action", ""),
            }
        )
        return ev

    def recheck_offer(self, offer_id: str, qty: int) -> dict[str, Any]:
        oid = _normalize_offer_id(offer_id)
        q = int(qty)
        res = rules.recheck_offer(oid, q, self.run_id)
        self._emit(
            {
                "type": "offer_rechecked",
                "part_id": self._offer_to_part.get(oid),
                "offer_id": oid,
                "supplier_id": res.get("supplier_id"),
                "qty": q,
                "live_stock": res.get("live_stock"),
                "live_price": res.get("live_price"),
                "passed": bool(res.get("passed")),
                "reason": res.get("reason", ""),
            }
        )
        return res

    def place_order(self, part_id: str, offer_id: str, qty: int) -> dict[str, Any]:
        pid = _normalize_part_id(part_id)
        oid = _normalize_offer_id(offer_id)
        q = int(qty)
        res = rules.place_order(
            pid,
            oid,
            q,
            self.run_id,
            spend_limit=self.spend_limit,
            part_limits=self.part_limits,
        )
        self._emit(
            {
                "type": "order_result",
                "part_id": pid,
                "offer_id": oid,
                "qty": q,
                "status": res.get("status"),
                "po_id": res.get("po_id"),
                "total": res.get("total"),
                "spend_limit": res.get("spend_limit"),
                "supplier_id": res.get("supplier_id"),
                "min_order_qty": res.get("min_order_qty", 1),
                "bulk_applied": bool(res.get("bulk_applied")),
                "reason": res.get("reason", ""),
            }
        )
        if res.get("status") == "placed":
            self.orders_placed_this_run += 1
            if self.crash_after > 0 and self.orders_placed_this_run >= self.crash_after:
                raise RuntimeError("Simulated crash")
        return res

    def log_reasoning(self, part_id: str, text: str) -> dict[str, Any]:
        pid = _normalize_part_id(part_id)
        cleaned = str(text or "").strip()
        audit.log(self.run_id, pid, "agent_note", cleaned, {"source": "agent"})
        self._emit({"type": "reasoning_logged", "part_id": pid, "text": cleaned})
        return {"ok": True, "part_id": pid}

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        try:
            if name == "get_inventory_status":
                return self.get_inventory_status()
            if name == "evaluate_part":
                return self.evaluate_part(part_id=args["part_id"])
            if name == "recheck_offer":
                return self.recheck_offer(offer_id=args["offer_id"], qty=int(args["qty"]))
            if name == "place_order":
                return self.place_order(
                    part_id=args["part_id"],
                    offer_id=args["offer_id"],
                    qty=int(args["qty"]),
                )
            if name == "log_reasoning":
                return self.log_reasoning(part_id=args["part_id"], text=args["text"])
            return {"error": f"Unknown tool: {name}"}
        except (KeyError, ValueError, TypeError) as exc:
            return {"error": f"Invalid arguments for {name}: {exc}"}
