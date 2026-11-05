"""Runtime configuration. Secrets stay in the environment, never in the page."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREE_CREDITS = 5


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _opt_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _opt_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass
class Config:
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    public_root: Path = field(default_factory=lambda: ROOT)
    secret: str = ""
    ephemeral_secret: bool = False
    admin_emails: frozenset[str] = field(default_factory=frozenset)
    free_credits: int = FREE_CREDITS
    reservation_ttl_seconds: int = 900
    cookie_secure: bool = False
    cookie_name: str = "actd_session"
    session_seconds: int = 14 * 24 * 3600
    payments_mode: str = "disabled"  # disabled | fixture | live
    payment_provider: str | None = None
    payment_webhook_secret: str | None = None
    price_cents: int | None = None
    price_currency: str | None = None
    credit_pack: int | None = None
    model_cost_policy_path: Path | None = None
    allow_live_charges: bool = False
    engine_base_url: str | None = None
    engine_start_path: str | None = None
    engine_invocation_enabled: bool = False
    max_run_budget_usd: float | None = None
    contract_path: Path | None = None
    max_body_bytes: int = 65_536
    auth_limit: int = 10
    auth_window_seconds: int = 3600
    start_limit: int = 8
    start_window_seconds: int = 3600
    canonical_origin: str = "https://aicrashtestdummies.com"
    # Test seam. Production leaves this empty. Called only by the request
    # that created a reservation, before any engine I/O.
    before_engine: object | None = None
    after_dispatch: object | None = None
    engine_transport: object | None = None
    operator_token: str | None = None
    require_verified_email: bool = False
    trusted_proxies: frozenset[str] = field(default_factory=frozenset)
    trusted_proxy_hops: int = 0
    signup_limit: int = 40
    form_cookie_name: str = "actd_form"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "actd.sqlite"

    @property
    def resolved_contract_path(self) -> Path:
        if self.contract_path is not None:
            return self.contract_path
        return self.public_root / "engine" / "contract.json"

    def payments_ready(self) -> bool:
        """True only when an operator supplied every billing fact.

        Fixture mode is a local deterministic signer for tests. Live mode
        still refuses to charge unless ACTD_ALLOW_LIVE_CHARGES=1, and this
        build does not call a payment network either way.
        """
        if self.payments_mode != "live":
            return False
        if self.payments_mode == "live" and not self.allow_live_charges:
            return False
        if self.payments_mode == "live" and not self.model_cost_recorded():
            return False
        if not self.payment_provider:
            return False
        if not self.payment_webhook_secret:
            return False
        if self.price_cents is None or self.price_cents <= 0:
            return False
        if not self.price_currency:
            return False
        if self.credit_pack is None or self.credit_pack <= 0:
            return False
        return True

    def model_cost_recorded(self) -> bool:
        """True only when an operator has filed a non-empty cost note.

        This repository has no model rate card. The file is not created here,
        and its contents are not logged.
        """
        path = self.model_cost_policy_path
        if path is None or not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return bool(text.strip())

    @classmethod
    def from_env(cls) -> "Config":
        import secrets as _secrets

        env = os.environ.get("ACTD_ENV", "development").strip().lower() or "development"
        secret = os.environ.get("ACTD_SESSION_SECRET", "").strip()
        ephemeral = False
        if not secret:
            if env == "production":
                raise SystemExit("ACTD_SESSION_SECRET is required when ACTD_ENV=production")
            secret = _secrets.token_hex(32)
            ephemeral = True
        admins = frozenset(
            item.strip().lower()
            for item in os.environ.get("ACTD_ADMIN_EMAILS", "").split(",")
            if item.strip()
        )
        mode = os.environ.get("ACTD_PAYMENTS_MODE", "disabled").strip().lower() or "disabled"
        if mode not in {"disabled", "fixture", "live"}:
            mode = "disabled"
        provider = os.environ.get("ACTD_PAYMENT_PROVIDER", "").strip().lower() or None
        webhook = os.environ.get("ACTD_PAYMENT_WEBHOOK_SECRET", "").strip() or None
        currency = os.environ.get("ACTD_PRICE_CURRENCY", "").strip().upper() or None
        policy = os.environ.get("ACTD_MODEL_COST_POLICY", "").strip()
        contract = os.environ.get("ACTD_CONTRACT_PATH", "").strip()
        data_dir = Path(os.environ.get("ACTD_DATA_DIR", str(ROOT / "data"))).expanduser()
        host = os.environ.get("ACTD_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.environ.get("ACTD_PORT", "8080"))
        cfg = cls(
            env=env,
            host=host,
            port=port,
            data_dir=data_dir,
            secret=secret,
            ephemeral_secret=ephemeral,
            admin_emails=admins,
            # The included allowance is a fixed product rule, not an env knob.
            free_credits=FREE_CREDITS,
            reservation_ttl_seconds=int(os.environ.get("ACTD_RESERVATION_TTL_SECONDS", "900")),
            cookie_secure=_flag("ACTD_COOKIE_SECURE", "1" if env == "production" else "0"),
            payments_mode=mode,
            payment_provider=provider,
            payment_webhook_secret=webhook,
            price_cents=_opt_int("ACTD_PRICE_CENTS"),
            price_currency=currency,
            credit_pack=_opt_int("ACTD_CREDIT_PACK"),
            model_cost_policy_path=Path(policy).expanduser() if policy else None,
            contract_path=Path(contract).expanduser() if contract else None,
            allow_live_charges=_flag("ACTD_ALLOW_LIVE_CHARGES"),
            operator_token=os.environ.get("ACTD_OPERATOR_TOKEN", "").strip() or None,
            require_verified_email=env == "production",
            trusted_proxies=frozenset(
                item.strip()
                for item in os.environ.get("ACTD_TRUSTED_PROXIES", "").split(",")
                if item.strip()
            ),
            trusted_proxy_hops=int(os.environ.get("ACTD_TRUSTED_PROXY_HOPS", "0") or "0"),
            signup_limit=int(os.environ.get("ACTD_SIGNUP_LIMIT", "5")),
            engine_base_url=os.environ.get("ACTD_ENGINE_BASE_URL", "").strip() or None,
            engine_start_path=os.environ.get("ACTD_ENGINE_START_PATH", "").strip() or None,
            engine_invocation_enabled=_flag("ACTD_ENGINE_INVOCATION_ENABLED"),
            max_run_budget_usd=_opt_float("ACTD_MAX_RUN_BUDGET_USD"),
            canonical_origin=os.environ.get("ACTD_CANONICAL_ORIGIN", "https://aicrashtestdummies.com").rstrip("/"),
        )
        if env == "production" and cfg.payments_mode == "fixture":
            raise SystemExit("ACTD_PAYMENTS_MODE=fixture is refused when ACTD_ENV=production")
        return cfg
