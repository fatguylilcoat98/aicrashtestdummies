"""Payment adapter.

No price, provider, or Stripe account exists in this repo. Checkout stays
closed until every required setting is present. Card numbers are never
accepted here. Live mode does not call a payment network in this build;
it only verifies signed webhooks the operator has decided to trust.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from app.config import Config

SIGNATURE_HEADER = "x-actd-signature"
MAX_SKEW_SECONDS = 300


@dataclass
class WebhookDecision:
    ok: bool
    status: int
    error: str | None = None
    event_id: str | None = None
    user_id: str | None = None
    credits: int | None = None
    duplicate: bool = False


def public_status(config: Config) -> dict:
    ready = config.payments_ready()
    price = None
    if ready and config.price_cents is not None and config.price_currency:
        price = {"cents": config.price_cents, "currency": config.price_currency, "credits": config.credit_pack}
    return {
        "enabled": ready,
        "mode": config.payments_mode if ready else "disabled",
        "provider": config.payment_provider if ready else None,
        "price": price,
        "collects_card_numbers": False,
        "reason": None
        if ready
        else _disabled_reason(config),
    }


def _disabled_reason(config: Config) -> str:
    if config.payments_mode == "live" and config.price_cents and not config.model_cost_recorded():
        return (
            "Checkout is off. A customer price is not enough to open it. "
            "This repository has no model rate card, and no model cost policy is on file."
        )
    return (
        "Checkout is off. No customer price and no payment provider are configured. "
        "This repository has no model rate card, so a paid test cannot be offered yet."
    )


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def header_value(timestamp: int, signature: str) -> str:
    return f"t={timestamp},v1={signature}"


def verify_signature(secret: str, header: str, body: bytes, now_epoch: int) -> bool:
    parts: dict[str, str] = {}
    for piece in (header or "").split(","):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        parts[key.strip()] = value.strip()
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    stamp = int(timestamp)
    if abs(now_epoch - stamp) > MAX_SKEW_SECONDS:
        return False
    expected = sign(secret, stamp, body)
    return hmac.compare_digest(expected, signature)


def parse_event(config: Config, body: bytes, header: str, now_epoch: int) -> WebhookDecision:
    if not config.payments_ready() or not config.payment_webhook_secret:
        return WebhookDecision(ok=False, status=503, error="payments_disabled")
    if not verify_signature(config.payment_webhook_secret, header, body, now_epoch):
        return WebhookDecision(ok=False, status=400, error="bad_signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return WebhookDecision(ok=False, status=400, error="bad_json")
    if not isinstance(payload, dict):
        return WebhookDecision(ok=False, status=400, error="bad_json")
    event_type = payload.get("type")
    if event_type != "checkout.completed":
        return WebhookDecision(ok=True, status=200, error="ignored")
    event_id = payload.get("id")
    user_id = payload.get("user_id")
    credits = payload.get("credits")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
        return WebhookDecision(ok=False, status=400, error="bad_event")
    if not isinstance(user_id, str) or not user_id:
        return WebhookDecision(ok=False, status=400, error="bad_event")
    if credits != config.credit_pack:
        return WebhookDecision(ok=False, status=400, error="credit_pack_mismatch")
    return WebhookDecision(
        ok=True,
        status=200,
        event_id=event_id,
        user_id=user_id,
        credits=credits,
    )


def payload_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
