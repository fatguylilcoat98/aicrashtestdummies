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
ALLOWED_FIELDS = {"question", "run_id", "budget_usd"}


@dataclass
class EngineOutcome:
    accepted: bool
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
    if not config.engine_start_path:
        found.append("start_path_missing")
    elif not config.engine_start_path.startswith("/") or config.engine_start_path.startswith("//"):
        found.append("start_path_rejected")
    if config.max_run_budget_usd is None or config.max_run_budget_usd <= 0:
        found.append("budget_missing")
    contract_path = config.resolved_contract_path
    loaded = _load_contract(contract_path) if contract_path.is_file() else None
    if not contract_path.is_file():
        found.append("contract_missing")
    elif loaded is None:
        found.append("contract_rejected")
    elif config.engine_start_path and loaded["start"]["path"] != config.engine_start_path:
        found.append("start_path_mismatch")
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
        "contract_missing": "engine/contract.json is not in the repo. There is no sanctioned request schema.",
        "team_talk_absent": "The Team Talk process documented at /opt/team-talk is not on this host.",
        "contract_rejected": "The contract file is present but does not match the binding this app can send.",
        "engine_http_error": "The engine returned an error before acceptance. The credit was released.",
        "engine_unreachable": "The engine could not be reached. No acceptance was recorded, so the credit was released.",
        "engine_rejected": "The engine response did not include an acceptance id. The credit was released.",
    }.get(code, code)


def invoke(config: Config, *, run_id: str, question: str) -> EngineOutcome:
    reasons = blockers(config)
    # team_talk_absent is reported, but a remote base URL can still be a
    # later deployment. Every other blocker refuses the call.
    hard = [item for item in reasons if item != "team_talk_absent"]
    if hard:
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail="The investigation engine was not called.\n" + "\n".join(describe(item) for item in hard),
            blockers=reasons,
        )
    contract = _load_contract(config.resolved_contract_path)
    if contract is None:
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("contract_rejected"),
            blockers=reasons + ["contract_rejected"],
        )
    payload = _payload(contract, run_id=run_id, question=question, budget=config.max_run_budget_usd)
    url = config.engine_base_url.rstrip("/") + contract["start"]["path"]
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": run_id,
        },
    )
    try:
        opener = build_opener(_NoRedirect())
        with opener.open(request, timeout=3) as response:
            raw = response.read(64_000)
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_http_error"),
            blockers=[f"http_{exc.code}"],
        )
    except (URLError, TimeoutError, OSError):
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_unreachable"),
            blockers=["unreachable"],
        )
    if status >= 400:
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_http_error"),
            blockers=[f"http_{status}"],
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_rejected"),
            blockers=["bad_json"],
        )
    if not isinstance(parsed, dict):
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_rejected"),
            blockers=["bad_json"],
        )
    accept_field = contract["accept_field"]
    ref = parsed.get(accept_field)
    if not isinstance(ref, str) or not ref.strip():
        return EngineOutcome(
            accepted=False,
            code="engine_unavailable",
            detail=describe("engine_rejected"),
            result_json=json.dumps(parsed)[:20_000],
            blockers=["missing_acceptance"],
        )
    return EngineOutcome(
        accepted=True,
        code="accepted",
        detail="The engine accepted the run. This site did not write a report of its own.",
        engine_ref=ref.strip()[:200],
        result_json=json.dumps(parsed)[:20_000],
        blockers=reasons,
    )


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


def _payload(contract: dict, *, run_id: str, question: str, budget: float | None) -> dict:
    values = {"question": question, "run_id": run_id, "budget_usd": budget}
    return {key: values[key] for key in contract["request_fields"]}
