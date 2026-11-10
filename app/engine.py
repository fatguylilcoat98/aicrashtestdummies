"""Team Talk gate.

No request is sent unless an operator supplies a base URL, an explicit path,
a budget cap, invocation enabled, and a contract file. This repository does
not ship that contract. identity-map.json is not an API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.config import Config

BINDING_SCHEMA = "actd.engine-binding/1"
ALLOWED_FIELDS = {"question", "run_id"}
AMBIGUOUS_HTTP = {408, 429, 500, 502, 503, 504}


@dataclass
class EngineOutcome:
    accepted: bool
    disposition: str
    code: str
    detail: str
    engine_ref: str | None = None
    result_json: str | None = None
    blockers: list[str] | None = None


def blockers(config: Config) -> list[str]:
    found: list[str] = []
    if not config.engine_invocation_enabled:
        found.append("invocation_disabled")
    if not config.engine_base_url:
        found.append("base_url_missing")
    elif not _allowed_base(config.engine_base_url):
        found.append("base_url_rejected")
    if config.max_run_budget_usd is None or config.max_run_budget_usd <= 0:
        found.append("budget_missing")
    contract_path = config.resolved_contract_path
    loaded = _load_contract(contract_path) if contract_path.is_file() else None
    host = ""
    if config.engine_base_url and _allowed_base(config.engine_base_url):
        from urllib.parse import urlparse

        host = (urlparse(config.engine_base_url).hostname or "").lower()
    if host and host not in {"127.0.0.1", "localhost", "::1"}:
        found.append("public_tunnel_blocked")
    if loaded is not None and not _budget_enforced(loaded):
        found.append("budget_not_engine_enforced")
    if not Path("/opt/team-talk").exists():
        found.append("team_talk_absent")
    return found


def describe(code: str) -> str:
    return {
        "invocation_disabled": "Engine calls are switched off (ACTD_ENGINE_INVOCATION_ENABLED is not 1).",
        "base_url_missing": "No Team Talk base URL was found in this repo or environment.",
        "base_url_rejected": "The engine base URL is not an http(s) URL, so it was not called.",
        "start_path_missing": "No start path is configured. This app does not guess a Team Talk route.",
        "start_path_rejected": "The configured start path is not a single absolute path.",
        "start_path_mismatch": "The configured start path does not match the contract, so nothing was called.",
        "budget_missing": "No per-run budget cap is configured, so a model call is refused.",
        "budget_not_engine_enforced": "A local contract file does not say the engine enforces the budget, so nothing was called.",
        "public_tunnel_blocked": "The engine URL is not loopback. This shell does not call the public tunnel.",
        "boundary_token_unconfigured": "AICTD_BOUNDARY_TOKEN is unset, so the account boundary was not called.",
        "contract_missing": "No local contract file is required. Team Talk's live OpenAPI is the contract.",
        "team_talk_absent": "The Team Talk process documented at /opt/team-talk is not on this host.",
        "contract_rejected": "The contract file is present but does not match the binding this app can send.",
        "engine_http_error": "The engine returned a complete rejection. The credit was released.",
        "engine_unreachable": "The connection was refused before the request was sent. The credit was released.",
        "engine_ambiguous": "The reply was lost or incomplete. The credit stays held because the engine may still be working.",
        "engine_rejected": "The engine response did not include an acceptance id. The credit was released.",
    }.get(code, code)


def invoke(config: Config, *, run_id: str, question: str, owner_id: str = "", idempotency_key: str = "") -> EngineOutcome:
    reasons = blockers(config)
    hard = [item for item in reasons if item != "team_talk_absent"]
    if hard:
        return EngineOutcome(
            accepted=False,
            disposition="not_sent",
            code="engine_unavailable",
            detail="The account boundary was not called.\n" + "\n".join(describe(item) for item in hard),
            blockers=reasons,
        )
    if config.engine_transport is not None:
        return config.engine_transport(run_id=run_id, question=question)
    from app.boundary_client import submit

    return submit(config, owner_id=owner_id, shell_key=idempotency_key or run_id, question=question)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _allowed_base(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _load_contract(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != BINDING_SCHEMA:
        return None
    start = data.get("start")
    if not isinstance(start, dict) or start.get("method") != "POST":
        return None
    path_value = start.get("path")
    if not isinstance(path_value, str) or not path_value.startswith("/") or path_value.startswith("//"):
        return None
    fields = data.get("request_fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(item, str) for item in fields):
        return None
    if any(item not in ALLOWED_FIELDS for item in fields):
        return None
    accept = data.get("accept_field")
    if not isinstance(accept, str) or not accept:
        return None
    return data


def _payload(contract: dict, *, run_id: str, question: str) -> dict:
    values = {"question": question, "run_id": run_id}
    return {key: values[key] for key in contract["request_fields"]}


def _budget_enforced(contract: dict) -> bool:
    budget = contract.get("budget")
    return isinstance(budget, dict) and budget.get("enforced_by") == "engine"


def _ambiguous(reason: str) -> EngineOutcome:
    return EngineOutcome(
        accepted=False,
        disposition="ambiguous",
        code="unknown",
        detail=describe("engine_ambiguous"),
        blockers=[reason],
    )


def _refused(exc: URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in {111, 61}:
        return True
    text = str(reason).lower()
    return "refused" in text or "errno 111" in text
