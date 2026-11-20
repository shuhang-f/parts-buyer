"""The five agent tools wrapping rules.py and audit.py plus OpenAI tool-calling schemas.

All money, compliance, recheck, and idempotency rules live in rules.py.
This module only exposes thin wrappers and schemas for agent.py.
"""

from __future__ import annotations

import os
from typing import Any

import audit
import rules

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
                "Always call this before place_order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offer_id": {
                        "type": "string",
                        "description": "Offer ID such as O1, O2, ..., O10",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "Quantity needed from evaluate_part",
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
                "Submit an order for a part using an offer that passed recheck_offer. "
                "ALWAYS call this even if landed cost exceeds the owner's spending limit: "
                "rules.py enforces approved suppliers, deadline, quantity, live recheck, "
                "idempotency, and routes orders over the limit into pending_approval for the owner."
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


class ToolSession:
    """Binds one agent run's run_id, resolved spend_limit, and crash switch to the 5 tools."""

    def __init__(self, *, run_id: str, spend_limit: float) -> None:
        self.run_id = run_id
        self.spend_limit = float(spend_limit)
        self.crash_after = _crash_after()
        self.orders_placed_this_run = 0

    def get_inventory_status(self) -> list[dict[str, Any]]:
        return rules.get_inventory_status()

    def evaluate_part(self, part_id: str) -> dict[str, Any]:
        return rules.evaluate_part(part_id, self.run_id, spend_limit=self.spend_limit)

    def recheck_offer(self, offer_id: str, qty: int) -> dict[str, Any]:
        return rules.recheck_offer(offer_id, int(qty), self.run_id)

    def place_order(self, part_id: str, offer_id: str, qty: int) -> dict[str, Any]:
        res = rules.place_order(
            part_id,
            offer_id,
            int(qty),
            self.run_id,
            spend_limit=self.spend_limit,
        )
        if res.get("status") == "placed":
            self.orders_placed_this_run += 1
            if self.crash_after > 0 and self.orders_placed_this_run >= self.crash_after:
                raise RuntimeError("Simulated crash")
        return res

    def log_reasoning(self, part_id: str, text: str) -> dict[str, Any]:
        audit.log(self.run_id, part_id, "agent_note", text.strip(), {"source": "agent"})
        return {"ok": True, "part_id": part_id}

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "get_inventory_status":
            return self.get_inventory_status()
        if name == "evaluate_part":
            return self.evaluate_part(part_id=str(args["part_id"]))
        if name == "recheck_offer":
            return self.recheck_offer(offer_id=str(args["offer_id"]), qty=int(args["qty"]))
        if name == "place_order":
            return self.place_order(
                part_id=str(args["part_id"]),
                offer_id=str(args["offer_id"]),
                qty=int(args["qty"]),
            )
        if name == "log_reasoning":
            return self.log_reasoning(part_id=str(args["part_id"]), text=str(args["text"]))
        return {"error": f"Unknown tool: {name}"}
