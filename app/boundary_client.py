"""Client for the Team Talk account boundary, /api/aict/v1.

Contract: Team-talk PR 59, commit f60a8dc, deploy/aict-boundary/CONTRACT.md.
The shell calls only loopback. The owner id comes from the server session.
HTTP 200 is a terminal record, not a passed crash test.
"""

from __future__ import annotations

import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.config import Config
from app.engine import EngineOutcome

BOUNDARY_PREFIX = "/api/aict/v1"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
BILLABLE_ROUTES = (
    "POST /api/chat",
    "POST /api/lab/crash/{campaign_id}/act",
    "POST /api/lab/experiments/{experiment_id}/authorize",
    "POST /api/lab/experiments/{experiment_id}/execute",
    "POST /api/lab/bridge/crash-test",
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def boundary_key(shell_key: str) -> str:
    """Stable engine key. Same shell key always produces the same value."""
    digest = hashlib.sha256(shell_key.encode("utf-8")).hexdigest()
    return "k" + digest[:40]


def endpoint(config: Config) -> str | None:
    raw = (config.engine_base_url or "").strip().rstrip("/")
    if not raw:
        return None
    if raw.endswith(BOUNDARY_PREFIX):
        return raw
    return raw + BOUNDARY_PREFIX


def host_allowed(config: Config) -> bool:
    base = config.engine_base_url or ""
    host = (urlparse(base).hostname or "").lower()
    return host in LOOPBACK


def classify(http_status: int, body: dict | None) -> str:
    """release, hold, commit, or retry. Commit means a provider call was recorded."""
    body = body or {}
    if http_status == 429:
        return "retry"
    if http_status in {400, 401, 403, 409, 503}:
        return "release"
    if http_status != 200:
        return "hold"
    status = body.get("status")
    if status == "uncertain":
        return "hold"
    if status in {"accepted", "running"}:
        return "hold"
    if body.get("error_code") == "cost_policy_absent":
        return "release"
    if status in {"failed", "cancelled", "expired"} and not body.get("model_invoked") and not body.get("reservation_held"):
        return "release"
    if body.get("model_invoked"):
        return "commit"
    if body.get("reservation_held"):
        return "hold"
    return "release"


def submit(config: Config, *, owner_id: str, shell_key: str, question: str) -> EngineOutcome:
    if not host_allowed(config):
        return _outcome(
            "release",
            "The engine URL is not loopback. This shell does not call the public tunnel.",
            blockers=["public_tunnel_blocked"],
        )
    token = config.boundary_token or ""
    if not token:
        return _outcome(
            "release",
            "AICTD_BOUNDARY_TOKEN is unset. No run was sent.",
            blockers=["boundary_token_unconfigured"],
        )
    url = endpoint(config)
    if not url:
        return _outcome("release", "No engine URL is configured.", blockers=["base_url_missing"])
    key = boundary_key(shell_key)
    cap = int(getattr(config, "input_char_cap", 12000) or 12000)
    text = question.strip()[:cap]
    payload = {
        "owner_id": owner_id,
        "title": (text[:80] or "Crash test"),
        "objective": text,
        "document_text": text,
        "content_type": "text/plain",
    }
    try:
        status, body = _json_request(
            url + "/runs",
            method="POST",
            payload=payload,
            token=token,
            owner_id=owner_id,
            idempotency_key=key,
        )
    except _Transport as exc:
        return exc.outcome
    if status == 200 and isinstance(body, dict) and body.get("report_ready") and body.get("run_id"):
        try:
            report_status, report = _json_request(
                url + "/runs/" + str(body["run_id"]) + "/report",
                method="GET",
                payload=None,
                token=token,
                owner_id=owner_id,
                idempotency_key=None,
            )
        except _Transport:
            report_status, report = 0, None
        if report_status == 200 and isinstance(report, dict):
            body = dict(body)
            body["report"] = {
                "schema": report.get("schema"),
                "crash_result": report.get("crash_result", "not_confirmed"),
                "acceptance": report.get("acceptance"),
                "findings_confirmed": report.get("findings_confirmed") or [],
                "raw_model_text_untrusted": report.get("raw_model_text_untrusted"),
            }
            if isinstance(report.get("findings_confirmed"), list):
                body["findings_confirmed"] = report["findings_confirmed"]
    return _from_body(status, body if isinstance(body, dict) else {})


def get_run(config: Config, *, owner_id: str, run_id: str) -> tuple[int, dict]:
    url = endpoint(config)
    if not url or not host_allowed(config) or not config.boundary_token:
        return 503, {"error": "boundary_token_unconfigured", "model_invoked": False}
    try:
        return _json_request(
            url + "/runs/" + run_id,
            method="GET",
            payload=None,
            token=config.boundary_token,
            owner_id=owner_id,
            idempotency_key=None,
        )
    except _Transport as exc:
        return 503, {"error": "unreachable", "detail": exc.outcome.detail}


def _from_body(status: int, body: dict) -> EngineOutcome:
    action = classify(status, body)
    code = str(body.get("error_code") or body.get("error") or body.get("status") or "boundary")
    findings = body.get("findings_confirmed")
    confirmed = isinstance(findings, list) and any(str(item).strip() for item in findings)
    if action == "commit" and confirmed:
        shell_code = "completed"
        detail = "The boundary confirmed a finding."
    elif action == "commit":
        shell_code = "finished_unconfirmed"
        detail = (
            "The boundary finished a bounded attempt. "
            "crash_result is not_confirmed and no finding was confirmed. "
            "A provider call was recorded, so this uses one included test."
        )
    elif action == "hold":
        shell_code = "unknown"
        detail = (
            "The boundary marked this run uncertain or unfinished. "
            "The credit stays held for operator review and this work is not retried."
        )
    elif action == "retry":
        shell_code = "retry_later"
        detail = "The boundary did not start a run (concurrency limit). The same idempotency key can be sent later."
    else:
        shell_code = "not_counted"
        if body.get("error_code") == "cost_policy_absent" or body.get("error") == "cost_policy_absent":
            detail = "No provider was called because the engine cost policy is absent. This did not use a free test."
        elif status == 400:
            detail = "The boundary rejected the input. No provider was called. This did not use a free test."
        else:
            detail = "No provider call was recorded. This did not use a free test."
            if code and code not in {"boundary", "not_counted"}:
                detail = detail + " (" + code + ")"
    disposition = {"commit": "accepted", "hold": "ambiguous", "retry": "retry", "release": "rejected"}[action]
    return EngineOutcome(
        accepted=action == "commit",
        disposition=disposition,
        code=shell_code,
        detail=detail,
        engine_ref=(str(body.get("run_id"))[:200] if body.get("run_id") else None),
        result_json=json.dumps(body)[:20000],
        blockers=[code] if action != "commit" else [],
    )


def _outcome(action: str, detail: str, blockers: list[str]) -> EngineOutcome:
    disposition = {"commit": "accepted", "hold": "ambiguous", "retry": "retry"}.get(action, "rejected")
    return EngineOutcome(
        accepted=False,
        disposition=disposition,
        code="not_counted" if action == "release" else "unknown",
        detail=detail,
        blockers=blockers,
    )


class _Transport(Exception):
    def __init__(self, outcome: EngineOutcome):
        self.outcome = outcome


def _json_request(url, *, method, payload, token, owner_id, idempotency_key):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-AICT-Owner": owner_id,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(url, data=data, method=method, headers=headers)
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            raw = response.read(64_000)
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        raw = exc.read(64_000)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            parsed = {"error": "invalid_body", "model_invoked": False}
        if not isinstance(parsed, dict):
            parsed = {"error": "invalid_body", "model_invoked": False}
        return exc.code, parsed
    except TimeoutError as exc:
        raise _Transport(
            EngineOutcome(
                accepted=False,
                disposition="ambiguous",
                code="unknown",
                detail="The boundary reply was lost. The credit stays held and this work is not retried.",
                blockers=["timeout"],
            )
        ) from exc
    except (URLError, OSError) as exc:
        raise _Transport(
            EngineOutcome(
                accepted=False,
                disposition="rejected",
                code="not_counted",
                detail="The boundary was not reached. No provider call was made.",
                blockers=["unreachable"],
            )
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _Transport(
            EngineOutcome(
                accepted=False,
                disposition="ambiguous",
                code="unknown",
                detail="The boundary returned an unreadable body. The credit stays held.",
                blockers=["bad_json"],
            )
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed
