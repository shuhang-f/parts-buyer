"""Parts Buyer agent: Plan A (tool-calling via OpenRouter or Google AI Studio) and Plan B (rules-first + explainer).

All money and compliance rules live in rules.py. This module resolves the spending limit once per run
via rules.get_spend_limit(spend_limit), tells the model that exact limit, passes it to tools.py,
streams live progress events via an optional on_event callback, and verifies DB completion before finishing.
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
from tools import TOOLS_SCHEMA, EventCallback, ToolSession

ROOT = Path(__file__).resolve().parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"

SYSTEM_PROMPT_TEMPLATE = """You are Parts Buyer, the purchasing agent for an equipment service shop.
Today is {TODAY}. {LIMIT_RULE}

Your job on each run:
1. Call get_inventory_status.
2. Process every part in part_id order (P01, P02, P03, P04, P05, P06, P07, P08).
   For every part, call evaluate_part, even if it looks fine.
3. If evaluate_part says the part needs buying and has eligible_offers, take the lowest landed-cost
   eligible offer and call recheck_offer using that offer's qty (which already includes any minimum
   order batch / bulk discount). If the recheck fails (passed=false), try the next eligible offer.
   Never skip the recheck.
4. ALWAYS call place_order for the offer that passed the recheck using that offer's qty — EVEN IF
   landed cost exceeds a part's spending limit. The place_order tool enforces limits in code and creates
   the pending_approval request for the owner; never skip calling place_order for over-limit items.
5. For every part you acted on (placed or pending_approval), call log_reasoning with two or three
   plain sentences: what triggered the purchase, which offer won and why (mention any minimum batch
   or bulk discount if used), and what you rejected and why. Write for a shop owner, not an engineer.

Rules you cannot change: only approved suppliers, never order twice, and
anything over a part's spending limit waits for the owner. If a tool refuses
something, accept it and explain it. Never invent prices, stock, PO numbers, or parts.
NEVER claim an order was placed unless you actually called place_order and received its tool response.
When every part is handled, reply with a short summary of the run."""


def _emit(on_event: EventCallback | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:
            pass


def _get_llm_config(model_override: str | None = None) -> tuple[str, str, str, str]:
    """Resolve (provider, api_key, base_url, model) supporting OpenRouter and Google AI Studio."""
    load_dotenv(ROOT / ".env", override=False)
    provider_pref = os.getenv("LLM_PROVIDER", "").strip().lower()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    env_model = os.getenv("MODEL", "").strip()

    if provider_pref == "openrouter" or (openrouter_key and not gemini_key):
        model = (model_override or env_model or DEFAULT_OPENROUTER_MODEL).strip()
        return "openrouter", openrouter_key, OPENROUTER_BASE_URL, model

    if gemini_key:
        default_gemini = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
        chosen = model_override or (env_model if env_model and "/" not in env_model else default_gemini)
        return "gemini", gemini_key, GEMINI_OPENAI_BASE_URL, chosen.strip()

    return "none", "", OPENROUTER_BASE_URL, (model_override or env_model or DEFAULT_OPENROUTER_MODEL)


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


def _serialize_assistant_message(msg: Any) -> dict[str, Any]:
    """Build a provider-safe assistant message preserving tool_calls and Gemini extra_content."""
    raw = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
    out: dict[str, Any] = {
        "role": "assistant",
        "content": raw.get("content") if raw.get("content") is not None else "",
    }
    if raw.get("tool_calls"):
        clean_calls: list[dict[str, Any]] = []
        for tc in raw["tool_calls"]:
            fn = tc.get("function") or {}
            item: dict[str, Any] = {
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": {
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments", "{}"),
                },
            }
            if tc.get("extra_content") is not None:
                item["extra_content"] = tc["extra_content"]
            clean_calls.append(item)
        out["tool_calls"] = clean_calls
    if raw.get("extra_content") is not None:
        out["extra_content"] = raw["extra_content"]
    return out


def _chat_with_resilience(
    client: OpenAI,
    *,
    provider: str,
    active_model: str,
    fallback_model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: int = 30,
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


def _incomplete_parts(run_id: str, all_part_ids: list[str]) -> tuple[list[str], list[str]]:
    """Return (missing_eval_parts, unfinished_gap_parts) according to the actual DB audit log."""
    entries = audit.read_by_run(run_id)
    evaluated = {
        e["part_id"]
        for e in entries
        if e["part_id"] and e["step"] in ("checked", "skipped", "gap_found")
    }
    terminal = {
        e["part_id"]
        for e in entries
        if e["part_id"]
        and e["step"] in ("checked", "skipped", "order_placed", "approval_requested", "no_viable_offer")
    }
    missing = [pid for pid in all_part_ids if pid not in evaluated]
    unfinished = [pid for pid in all_part_ids if pid in evaluated and pid not in terminal]
    return missing, unfinished


def _fallback_note(part_id: str, entries: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    for step in ("gap_found", "rejected_option", "recheck", "order_placed", "approval_requested", "no_viable_offer"):
        msgs = [e["message"] for e in entries if e["step"] == step]
        if msgs:
            bits.append(msgs[0] if step != "rejected_option" else " ".join(msgs[:2]))
    return " ".join(bits) if bits else f"Handled {part_id} per purchasing policy."


def _complete_remaining_parts(
    session: ToolSession,
    run_id: str,
    missing_parts: list[str],
    unfinished_parts: list[str],
    on_event: EventCallback | None = None,
) -> list[str]:
    """Safety net: finish any parts the LLM skipped or left mid-flight, using ToolSession."""
    recovered: list[str] = []
    entries = audit.read_by_run(run_id)
    recheck_passed: set[str] = {
        e["data"].get("offer_id")
        for e in entries
        if e["step"] == "recheck" and e["data"].get("passed") and e["data"].get("offer_id")
    }
    recheck_failed: set[str] = {
        e["data"].get("offer_id")
        for e in entries
        if e["step"] == "recheck" and not e["data"].get("passed") and e["data"].get("offer_id")
    }

    for pid in missing_parts + unfinished_parts:
        recovered.append(pid)
        if pid in missing_parts:
            ev = session.evaluate_part(pid)
        else:
            ev = rules._evaluate(pid, spend_limit=session.spend_limit, part_limits=session.part_limits)
            for o in ev.get("eligible_offers", []):
                if o.get("offer_id"):
                    session._offer_to_part[o["offer_id"]] = pid

        if ev.get("existing_order") or not ev.get("needs_buying"):
            continue

        placed_or_pending = False
        for offer in ev.get("eligible_offers", []):
            oid = offer["offer_id"]
            order_qty = int(offer.get("qty", ev["qty"]))
            if oid in recheck_failed:
                continue
            if oid not in recheck_passed:
                rc = session.recheck_offer(oid, order_qty)
                if not rc.get("passed"):
                    recheck_failed.add(oid)
                    continue
                recheck_passed.add(oid)
            res = session.place_order(pid, oid, order_qty)
            if res.get("status") in ("placed", "pending_approval", "exists"):
                placed_or_pending = True
                break

        if not placed_or_pending and ev.get("eligible_offers"):
            audit.log(
                run_id,
                pid,
                "no_viable_offer",
                "Every eligible offer failed the recheck. Nothing ordered; the owner should look at this part.",
                {"qty": ev["qty"]},
            )
            _emit(
                on_event,
                {
                    "type": "no_viable_offer",
                    "part_id": pid,
                    "reason": "Every eligible offer failed the live recheck.",
                },
            )

    return recovered


def _ensure_agent_notes(session: ToolSession, run_id: str) -> None:
    """Ensure every placed/pending_approval part in this run has a plain-English agent_note."""
    entries = audit.read_by_run(run_id)
    acted = sorted(
        {
            e["part_id"]
            for e in entries
            if e["part_id"] and e["step"] in ("order_placed", "approval_requested")
        }
    )
    noted = {e["part_id"] for e in entries if e["part_id"] and e["step"] == "agent_note"}
    for pid in acted:
        if pid not in noted:
            part_entries = [e for e in entries if e["part_id"] == pid]
            session.log_reasoning(pid, _fallback_note(pid, part_entries))


def _limit_rule_text(limit: float | None, part_limits: dict[str, float] | None) -> str:
    if part_limits is not None:
        if not part_limits:
            return "There is no global spending limit and no part-specific spending limits are set."
        pairs = ", ".join(f"{pid}: ${_format_limit(float(val))}" for pid, val in sorted(part_limits.items()))
        return (
            f"There is no global spending limit. Part-specific spending limits apply only to: {pairs}. "
            "All other parts have no spending cap."
        )
    return f"The owner's spending limit per order is ${_format_limit(float(limit or 0))}."


def run_agent(
    spend_limit: float | None = None,
    *,
    model: str | None = None,
    max_turns: int = 40,
    on_event: EventCallback | None = None,
    part_limits: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Plan A: tool-calling agent wrapping rules.py and audit.py (up to 40 turns)."""
    provider, api_key, base_url, model_name = _get_llm_config(model)
    if not api_key:
        raise ValueError(
            "Set OPENROUTER_API_KEY or GEMINI_API_KEY in .env to run Agent mode (or switch to Rules only)."
        )

    run_id = rules.start_run()
    limit = None if part_limits is not None else rules.get_spend_limit(spend_limit)
    today_str = db.today()
    all_part_ids = [p["part_id"] for p in rules.get_inventory_status()]

    _emit(
        on_event,
        {
            "type": "run_started",
            "run_id": run_id,
            "mode": "agent",
            "provider": provider,
            "model": model_name,
            "spend_limit": limit,
            "part_limits": part_limits,
            "today": today_str,
            "part_ids": all_part_ids,
        },
    )

    session = ToolSession(
        run_id=run_id,
        spend_limit=limit,
        part_limits=part_limits,
        on_event=on_event,
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        TODAY=today_str,
        LIMIT_RULE=_limit_rule_text(limit, part_limits),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Run today's ({today_str}) parts purchasing cycle across every part in part_id order "
                f"({', '.join(all_part_ids)}). Recheck every winning offer using that offer's qty "
                "before calling place_order, always call place_order even when over a part's spending "
                "limit, and call log_reasoning for each part acted on."
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
    nudges = 0
    final_reply = ""
    recovered_parts: list[str] = []

    for turns in range(1, max_turns + 1):
        _emit(on_event, {"type": "thinking", "turn": turns, "model": active_model})
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
        messages.append(_serialize_assistant_message(msg))

        if not msg.tool_calls:
            missing_parts, unfinished_parts = _incomplete_parts(run_id, all_part_ids)
            if (missing_parts or unfinished_parts) and nudges < 3 and turns < max_turns:
                nudges += 1
                _emit(
                    on_event,
                    {
                        "type": "nudge",
                        "turn": turns,
                        "missing_parts": missing_parts,
                        "unfinished_parts": unfinished_parts,
                    },
                )
                details: list[str] = []
                if missing_parts:
                    details.append(f"Not yet evaluated with evaluate_part: {', '.join(missing_parts)}.")
                if unfinished_parts:
                    details.append(
                        f"Evaluated as short but no order/approval recorded yet (call recheck_offer then place_order): {', '.join(unfinished_parts)}."
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Do not stop yet—the database shows unfinished work. "
                            + " ".join(details)
                            + " Call the required tools right now; do not describe orders in text without calling place_order."
                        ),
                    }
                )
                continue
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

    # Safety net: if a weak/flaky model still left parts unprocessed after nudges, complete them cleanly.
    missing_parts, unfinished_parts = _incomplete_parts(run_id, all_part_ids)
    if missing_parts or unfinished_parts:
        _emit(
            on_event,
            {
                "type": "safety_net",
                "missing_parts": missing_parts,
                "unfinished_parts": unfinished_parts,
            },
        )
        recovered_parts = _complete_remaining_parts(
            session, run_id, missing_parts, unfinished_parts, on_event=on_event
        )

    _ensure_agent_notes(session, run_id)

    memory_saved = None
    try:
        import memory

        if memory.is_enabled():
            memory.seed_policy(background=True)
            memory_saved = memory.remember_run_lessons(run_id, audit.read_by_run(run_id))
            if memory_saved.get("stored"):
                _emit(
                    on_event,
                    {
                        "type": "memory_saved",
                        "stored": memory_saved["stored"],
                        "dataset": memory_saved.get("dataset"),
                    },
                )
    except Exception:
        pass

    summary = _summarize_run_from_db(run_id, limit)
    summary.update(
        {
            "mode": "agent",
            "provider": provider,
            "model": active_model,
            "part_limits": part_limits,
            "memory_saved": memory_saved,
            "turns": turns,
            "nudges": nudges,
            "recovered_parts": recovered_parts,
            "elapsed_seconds": round(time.perf_counter() - start_ts, 2),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "final_reply": final_reply,
        }
    )
    _emit(on_event, {"type": "run_finished", "summary": summary})
    return summary


def run_explain_only(
    spend_limit: float | None = None,
    *,
    model: str | None = None,
    on_event: EventCallback | None = None,
    part_limits: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Plan B: run rules_only first, then make one model call per gap part to explain in plain English."""
    limit = None if part_limits is not None else rules.get_spend_limit(spend_limit)
    start_ts = time.perf_counter()
    summary = rules.run_rules_only(spend_limit=limit, part_limits=part_limits)
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
        _emit(on_event, {"type": "reasoning_logged", "part_id": part_id, "text": text})

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
    _emit(on_event, {"type": "run_finished", "summary": summary})
    return summary
