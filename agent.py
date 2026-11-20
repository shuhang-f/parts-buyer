"""Parts Buyer agent: Plan A (tool-calling via OpenRouter or Google AI Studio) and Plan B (rules-first + explainer).

All money and compliance rules live in rules.py. This module resolves the spending limit once per run
via rules.get_spend_limit(spend_limit), tells the model that exact limit, and passes it to tools.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

import audit
import db
import rules
from tools import TOOLS_SCHEMA, ToolSession

ROOT = Path(__file__).resolve().parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

SYSTEM_PROMPT_TEMPLATE = """You are Parts Buyer, the purchasing agent for an equipment service shop.
Today is {TODAY}. The owner's spending limit per order is ${SPEND_LIMIT}.

Your job on each run:
1. Call get_inventory_status.
2. Process every part in part_id order (P01, P02, P03, P04, P05, P06, P07, P08).
   For every part, call evaluate_part, even if it looks fine.
3. If evaluate_part says the part needs buying and has eligible_offers, take the lowest landed-cost
   eligible offer and call recheck_offer. If the recheck fails (passed=false), try the next eligible offer.
   Never skip the recheck.
4. ALWAYS call place_order for the offer that passed the recheck — EVEN IF landed cost exceeds
   the ${SPEND_LIMIT} spending limit. The place_order tool enforces the limit in code and creates
   the pending_approval request for the owner; never skip calling place_order for over-limit items.
5. For every part you acted on (placed or pending_approval), call log_reasoning with two or three
   plain sentences: what triggered the purchase, which offer won and why, and what you rejected
   and why. Write for a shop owner, not an engineer.

Rules you cannot change: only approved suppliers, never order twice, and
anything over the spending limit waits for the owner. If a tool refuses
something, accept it and explain it. Never invent prices, stock or parts.
When every part is handled, reply with a short summary of the run."""


def _get_llm_config(model_override: str | None = None) -> tuple[str, str, str, str]:
    """Resolve (provider, api_key, base_url, model) supporting OpenRouter and Google AI Studio."""
    load_dotenv(ROOT / ".env", override=False)
    provider_pref = os.getenv("LLM_PROVIDER", "").strip().lower()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    env_model = os.getenv("MODEL", "").strip()

    if provider_pref == "openrouter" or (openrouter_key and not gemini_key):
        model = (model_override or env_model or "openrouter/auto").strip()
        return "openrouter", openrouter_key, OPENROUTER_BASE_URL, model

    if gemini_key:
        default_gemini = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
        chosen = model_override or (env_model if env_model and "/" not in env_model else default_gemini)
        return "gemini", gemini_key, GEMINI_OPENAI_BASE_URL, chosen.strip()

    return "none", "", OPENROUTER_BASE_URL, (model_override or env_model or "openrouter/auto")


def _make_client(provider: str, api_key: str, base_url: str) -> OpenAI:
    headers = (
        {
            "HTTP-Referer": "https://parts-buyer.local",
            "X-Title": "Parts Buyer Agent",
        }
        if provider == "openrouter"
        else None
    )
    return OpenAI(api_key=api_key, base_url=base_url, default_headers=headers)


def _format_limit(limit: float) -> str:
    return f"{int(round(limit)):,}" if float(limit).is_integer() else f"{limit:,.2f}"


def _chat_with_resilience(
    client: OpenAI,
    *,
    provider: str,
    active_model: str,
    fallback_model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: int = 25,
) -> tuple[Any, str]:
    """Call chat completions with automatic 503/timeout fallback and polite 429 retry backoff."""
    model_now = active_model
    for attempt in range(4):
        try:
            kwargs: dict[str, Any] = {
                "model": model_now,
                "messages": messages,
                "temperature": 0,
                "timeout": timeout,
            }
            if tools is not None:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            resp = client.chat.completions.create(**kwargs)
            return resp, model_now
        except Exception as exc:
            err_txt = f"{type(exc).__name__} {exc}".lower()
            is_transient = any(k in err_txt for k in ("503", "unavailable", "timeout", "timed out"))
            is_rate_limit = "429" in err_txt or "rate" in err_txt
            if provider == "gemini" and fallback_model and model_now != fallback_model and (is_transient or is_rate_limit):
                model_now = fallback_model
                continue
            if (is_rate_limit or is_transient) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError("LLM call exhausted retries.")


def _summarize_run_from_db(run_id: str, limit: float) -> dict[str, Any]:
    entries = audit.read_by_run(run_id)
    placed = sorted({e["part_id"] for e in entries if e["step"] == "order_placed" and e["part_id"]})
    pending = sorted({e["part_id"] for e in entries if e["step"] == "approval_requested" and e["part_id"]})
    skipped = sorted({e["part_id"] for e in entries if e["step"] == "skipped" and e["part_id"]})
    checked = sorted({e["part_id"] for e in entries if e["step"] == "checked" and e["part_id"]})
    no_viable = sorted({e["part_id"] for e in entries if e["step"] == "no_viable_offer" and e["part_id"]})
    refused = sorted({e["part_id"] for e in entries if e["step"] == "refused" and e["part_id"]})
    return {
        "run_id": run_id,
        "spend_limit": limit,
        "today": db.today(),
        "placed": placed,
        "pending_approval": pending,
        "skipped": skipped,
        "checked": checked,
        "no_viable_offer": no_viable,
        "refused": refused,
    }


def run_agent(
    spend_limit: float | None = None,
    *,
    model: str | None = None,
    max_turns: int = 40,
) -> dict[str, Any]:
    """Plan A: tool-calling agent wrapping rules.py and audit.py (up to 40 turns)."""
    provider, api_key, base_url, model_name = _get_llm_config(model)
    if not api_key:
        raise ValueError(
            "Set OPENROUTER_API_KEY or GEMINI_API_KEY in .env to run Agent mode (or switch to Rules only)."
        )

    run_id = rules.start_run()
    limit = rules.get_spend_limit(spend_limit)  # once per run: model and code see identical limit
    today_str = db.today()

    session = ToolSession(run_id=run_id, spend_limit=limit)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        TODAY=today_str,
        SPEND_LIMIT=_format_limit(limit),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Run today's ({today_str}) parts purchasing cycle across every part in part_id order "
                "(P01 through P08). Recheck every winning offer before calling place_order, always call "
                "place_order even when over the spending limit, and call log_reasoning for each part acted on."
            ),
        },
    ]

    client = _make_client(provider, api_key, base_url)
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip() if provider == "gemini" else ""
    active_model = model_name
    start_ts = time.perf_counter()
    prompt_tokens = 0
    completion_tokens = 0
    turns = 0
    final_reply = ""

    for turns in range(1, max_turns + 1):
        response, active_model = _chat_with_resilience(
            client,
            provider=provider,
            active_model=active_model,
            fallback_model=fallback_model,
            messages=messages,
            tools=TOOLS_SCHEMA,
        )
        if response.usage:
            prompt_tokens += int(response.usage.prompt_tokens or 0)
            completion_tokens += int(response.usage.completion_tokens or 0)

        msg = response.choices[0].message
        # Preserve provider fields (e.g., Gemini 3/4 thought_signature in extra_content)
        assistant_msg: dict[str, Any] = msg.model_dump(exclude_none=True)
        assistant_msg.setdefault("role", "assistant")
        messages.append(assistant_msg)

        if not msg.tool_calls:
            final_reply = msg.content or ""
            break

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = session.dispatch(fn_name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": fn_name,
                    "content": json.dumps(result),
                }
            )

    summary = _summarize_run_from_db(run_id, limit)
    summary.update(
        {
            "mode": "agent",
            "provider": provider,
            "model": active_model,
            "turns": turns,
            "elapsed_seconds": round(time.perf_counter() - start_ts, 2),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "final_reply": final_reply,
        }
    )
    return summary


def _fallback_note(part_id: str, entries: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    for step in ("gap_found", "rejected_option", "recheck", "order_placed", "approval_requested", "no_viable_offer"):
        msgs = [e["message"] for e in entries if e["step"] == step]
        if msgs:
            bits.append(msgs[0] if step != "rejected_option" else " ".join(msgs[:2]))
    return " ".join(bits) if bits else f"Handled {part_id} per purchasing policy."


def run_explain_only(
    spend_limit: float | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Plan B: run rules_only first, then make one model call per gap part to explain in plain English."""
    limit = rules.get_spend_limit(spend_limit)
    start_ts = time.perf_counter()
    summary = rules.run_rules_only(spend_limit=limit)
    run_id = summary["run_id"]

    provider, api_key, base_url, model_name = _get_llm_config(model)
    client = _make_client(provider, api_key, base_url) if api_key else None
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip() if provider == "gemini" else ""
    active_model = model_name
    prompt_tokens = 0
    completion_tokens = 0
    notes: dict[str, str] = {}

    acted_parts = sorted(
        set(summary.get("placed", []))
        | set(summary.get("pending_approval", []))
        | set(summary.get("no_viable_offer", []))
    )

    for part_id in acted_parts:
        entries = audit.read_by_part(part_id, run_id=run_id)
        if client is not None:
            compact = [{"step": e["step"], "message": e["message"], "data": e["data"]} for e in entries]
            req_msgs = [
                {
                    "role": "system",
                    "content": (
                        "You are Parts Buyer explaining a purchasing decision to an equipment service shop owner. "
                        "Write 2 to 3 plain-English sentences: what triggered the purchase, which supplier won "
                        "and why, and what you rejected and why. Mention ONLY part IDs, suppliers, and dollar figures "
                        "present in the audit rows below. Never invent numbers or mention internal rule codes."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Audit rows for {part_id}:\n{json.dumps(compact, indent=2)}",
                },
            ]
            try:
                resp, active_model = _chat_with_resilience(
                    client,
                    provider=provider,
                    active_model=active_model,
                    fallback_model=fallback_model,
                    messages=req_msgs,
                    timeout=20,
                )
                if resp.usage:
                    prompt_tokens += int(resp.usage.prompt_tokens or 0)
                    completion_tokens += int(resp.usage.completion_tokens or 0)
                text = (resp.choices[0].message.content or "").strip() or _fallback_note(part_id, entries)
            except Exception:
                text = _fallback_note(part_id, entries)
        else:
            text = _fallback_note(part_id, entries)

        audit.log(run_id, part_id, "agent_note", text, {"source": "plan_b"})
        notes[part_id] = text

    summary.update(
        {
            "mode": "plan_b",
            "provider": provider,
            "model": active_model if client is not None else "grounded-local",
            "elapsed_seconds": round(time.perf_counter() - start_ts, 2),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "agent_notes": notes,
        }
    )
    return summary
