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
    if stripe_checkout_configured(config):
        return {
            "enabled": True,
            "mode": "stripe",
            "provider": "stripe",
            "price": None,
            "credits": config.stripe_credits,
            "collects_card_numbers": False,
            "reason": None,
            "payment_link": bool(config.stripe_payment_link),
        }
    return {
        "enabled": False,
        "mode": "disabled",
        "provider": None,
        "price": None,
        "collects_card_numbers": False,
        "reason": "Paid tests coming soon. No price is published, and this repository has no model rate card.",
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


def stripe_webhook_configured(config: Config) -> bool:
    return bool(config.stripe_webhook_secret and config.stripe_credits and config.stripe_credits > 0)


def stripe_checkout_configured(config: Config) -> bool:
    if not stripe_webhook_configured(config):
        return False
    if config.stripe_payment_link:
        return True
    return bool(config.stripe_secret_key and config.stripe_price_id)


def checkout_form(user_id: str, price_id: str, origin: str) -> bytes:
    base = origin.rstrip("/")
    from urllib.parse import urlencode

    return urlencode(
        {
            "mode": "payment",
            "client_reference_id": user_id,
            "success_url": f"{base}/app/billing?stripe=return",
            "cancel_url": f"{base}/app/billing?stripe=cancel",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
        }
    ).encode("utf-8")


def create_checkout_session(secret: str, user_id: str, price_id: str, origin: str) -> str | None:
    """Ask Stripe for a hosted Checkout URL. Card numbers never touch this server."""
    from urllib.request import Request, urlopen

    body = checkout_form(user_id, price_id, origin)
    request = Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(20_000).decode("utf-8"))
    except Exception:
        return None
    url = payload.get("url") if isinstance(payload, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def parse_stripe_event(config: Config, body: bytes, header: str, now_epoch: int) -> WebhookDecision:
    if not stripe_webhook_configured(config):
        return WebhookDecision(ok=False, status=503, error="payments_disabled")
    if not verify_signature(config.stripe_webhook_secret or "", header, body, now_epoch):
        return WebhookDecision(ok=False, status=400, error="bad_signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return WebhookDecision(ok=False, status=400, error="bad_json")
    if not isinstance(payload, dict):
        return WebhookDecision(ok=False, status=400, error="bad_json")
    if payload.get("type") != "checkout.session.completed":
        return WebhookDecision(ok=True, status=200, error="ignored")
    event_id = payload.get("id")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    if obj.get("payment_status") != "paid":
        return WebhookDecision(ok=True, status=200, error="ignored")
    user_id = obj.get("client_reference_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
        return WebhookDecision(ok=False, status=400, error="bad_event")
    if not isinstance(user_id, str) or not user_id:
        return WebhookDecision(ok=False, status=400, error="bad_event")
    return WebhookDecision(
        ok=True,
        status=200,
        event_id=event_id,
        user_id=user_id,
        credits=config.stripe_credits,
    )
