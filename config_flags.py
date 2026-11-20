"""Live rules from Statsig (SPEC A5, sponsor 1).

get_live_limit() -> (value, source)
    source is "Statsig" when the number came from the Statsig Dynamic Config
    parts_buyer_rules (field spend_limit), otherwise ".env" (SPEND_LIMIT).

This file only fetches a number. It never decides anything about money;
rules.py does. It never raises: any problem logs "sponsor_error" and falls
back to .env.

Freshness: one Statsig client per process, polling Statsig every 2 seconds
in the background, so a console change shows up within about 2 seconds.
SDK: statsig-python-core (import statsig_python_core).
"""

import logging
import math
import os
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

CONFIG_NAME = "parts_buyer_rules"
FIELD = "spend_limit"
SYNC_INTERVAL_MS = 2_000    # background poll; console changes land within ~2 s
INIT_TIMEOUT_MS = 3_000     # never hold up a run for long if Statsig is down
RETRY_AFTER_S = 30          # if the first connect failed, try again after this

log = logging.getLogger("parts_buyer.config_flags")

_lock = threading.Lock()
_client = None
_client_ready = False
_last_attempt = 0.0
_last_error = None


def _sponsor_error(reason):
    """Log once per distinct reason so a dead network doesn't flood the console."""
    global _last_error
    # The SDK's error text includes the download URL, which carries part of the secret key.
    reason = re.sub(r"https?://\S+", "<statsig url>", str(reason))
    reason = re.sub(r"secret-[\w*.-]+", "<secret>", reason)
    secret = os.getenv("STATSIG_SERVER_SECRET", "").strip()
    if secret:
        reason = reason.replace(secret, "<secret>")
    if reason != _last_error:
        log.warning("sponsor_error: statsig: %s", reason)
        _last_error = reason


def _env_limit():
    try:
        value = float(os.getenv("SPEND_LIMIT", "1000"))
        return value if math.isfinite(value) and value >= 0 else 1000.0
    except ValueError:
        return 1000.0


def _get_client(secret):
    """Start (or reuse) the Statsig client. Returns None if it cannot connect."""
    global _client, _client_ready, _last_attempt
    with _lock:
        if _client is not None and _client_ready:
            return _client
        if time.time() - _last_attempt < RETRY_AFTER_S and _client is None:
            return None
        _last_attempt = time.time()
        try:
            from statsig_python_core import Statsig, StatsigOptions

            if _client is not None:  # an earlier attempt never got config; start over
                try:
                    _client.shutdown().wait()
                except Exception:
                    pass
                _client = None
            options = StatsigOptions()
            options.init_timeout_ms = INIT_TIMEOUT_MS
            options.specs_sync_interval_ms = SYNC_INTERVAL_MS
            options.output_log_level = "none"
            client = Statsig(secret, options)
            details = client.initialize_with_details().result()
            # init_success can be True even with a bad key; is_config_spec_ready is the real test.
            if not details.is_config_spec_ready:
                reason = getattr(details.failure_details, "reason", None) or "no config downloaded"
                _sponsor_error(f"could not load config ({reason})")
                client.shutdown().wait()
                return None
            _client, _client_ready = client, True
            return _client
        except Exception as exc:
            _sponsor_error(f"init failed ({exc})")
            return None


def get_live_limit():
    """Return (spend_limit, source). Never raises."""
    fallback = (_env_limit(), ".env")
    try:
        if os.getenv("USE_STATSIG", "0").strip() != "1":
            return fallback
        secret = os.getenv("STATSIG_SERVER_SECRET", "").strip()
        if not secret:
            _sponsor_error("USE_STATSIG=1 but STATSIG_SERVER_SECRET is empty")
            return fallback
        client = _get_client(secret)
        if client is None:
            return fallback

        from statsig_python_core import StatsigUser

        config = client.get_dynamic_config(StatsigUser("shop-owner"), CONFIG_NAME)
        values = config.get_value() or {}
        reason = str(config.get_evaluation_details().to_dict().get("reason", ""))
        if FIELD not in values or "Unrecognized" in reason or reason.startswith("NoValues"):
            _sponsor_error(f"{CONFIG_NAME}.{FIELD} not found (reason {reason or 'unknown'})")
            return fallback
        value = float(values[FIELD])
        if not math.isfinite(value) or value < 0:
            _sponsor_error(f"{CONFIG_NAME}.{FIELD} must be a number of 0 or more (got {value})")
            return fallback
        return value, "Statsig"
    except Exception as exc:
        _sponsor_error(f"lookup failed ({exc})")
        return fallback


def get_spend_limit():
    """Just the number, for rules.py."""
    return get_live_limit()[0]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_live_limit())
