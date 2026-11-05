"""Team Talk crash adapter.

Verified against the live OpenAPI at teamtalk.thegoodneighborguard.com
(title Team Talk, 233 paths, GET /api/version = 78) and the engine's own
/crash page. This module does not contain a second investigation engine.

Opening a campaign is intake. The live modes payload says seats are recorded
and are not called by intake. A model call is a later instrument (`act`),
experiment execute, or `/api/chat`. Those stay closed: the engine has no
per-customer budget the shell can set before a provider call.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import Config
from app.engine import EngineOutcome, _ambiguous, _refused

TRACKS = ("CODE", "AI", "DOCUMENT", "OTHER")
PUBLIC_TUNNEL_HOST = "teamtalk.thegoodneighborguard.com"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
BILLABLE_ROUTES = (
    "POST /api/chat",
    "POST /api/lab/crash/{campaign_id}/act",
    "POST /api/lab/experiments/{experiment_id}/authorize",
    "POST /api/lab/experiments/{experiment_id}/execute",
    "POST /api/lab/bridge/crash-test",
)


def host_of(base_url: str | None) -> str:
    if not base_url:
        return ""
    return (urlparse(base_url).hostname or "").lower()


def allows_post(config: Config) -> bool:
    """Intake may be sent only to loopback. The public tunnel has no customer auth."""
    return host_of(config.engine_base_url) in LOOPBACK_HOSTS


def open_body(run_id: str, question: str) -> dict:
    text = question.strip()[:4000]
    return {
        "session_id": "actd-" + run_id[:20],
        "track": "DOCUMENT",
        "title": (text[:80] or "Crash test"),
        "objective": text,
        "focus": "CLAIMS",
        "intensity": "QUICK",
        "forbidden": [],
        "text": text,
        "local_path": None,
        "git": None,
        "zip_b64": None,
    }


def intake(config: Config, *, run_id: str, question: str) -> EngineOutcome:
    if not allows_post(config):
        return EngineOutcome(
            accepted=False,
            disposition="not_sent",
            code="engine_unavailable",
            detail=(
                "No campaign was opened. The configured engine host is not loopback. "
                f"{PUBLIC_TUNNEL_HOST} is a public tunnel with no customer auth, so this shell does not POST to it."
            ),
            blockers=["public_tunnel_blocked"],
        )
    base = config.engine_base_url.rstrip("/")
    try:
        version = _get_json(base + "/api/version")
        modes = _get_json(base + "/api/lab/crash/modes")
    except _CallError as exc:
        return exc.outcome
    if not isinstance(version, dict) or "version" not in version:
        return _rejected("The version endpoint did not return a version. Nothing was opened.")
    if not _intake_is_non_model(modes):
        return EngineOutcome(
            accepted=False,
            disposition="not_sent",
            code="engine_unavailable",
            detail="Crash modes did not confirm that intake leaves seats uncalled. No campaign was opened.",
            blockers=["modes_unconfirmed"],
        )
    body = open_body(run_id, question)
    try:
        opened = _post_json(base + "/api/lab/crash/open", body)
    except _CallError as exc:
        return exc.outcome
    if not isinstance(opened, dict) or opened.get("status") != "BOUND" or not opened.get("campaign_id"):
        return _rejected("Crash open did not return status BOUND with a campaign id. No model call was made.")
    campaign_id = str(opened["campaign_id"])[:200]
    try:
        state = _get_json(base + f"/api/lab/crash/{campaign_id}")
    except _CallError as exc:
        # The campaign may exist. Do not release as if nothing happened.
        return EngineOutcome(
            accepted=False,
            disposition="ambiguous",
            code="unknown",
            detail="The campaign open was sent, then its state could not be read. The credit stays held.",
            engine_ref=campaign_id,
            blockers=["state_unread"],
        )
    seats_called = bool(isinstance(state, dict) and state.get("seats_called"))
    summary = {
        "campaign_id": campaign_id,
        "open_status": "BOUND",
        "seats_called": seats_called,
        "seats_attached": (state.get("seats_attached") if isinstance(state, dict) else []) or [],
        "idle": bool(isinstance(state, dict) and state.get("idle")),
        "laboratory_status": (state.get("laboratory") or {}).get("status") if isinstance(state, dict) else None,
        "billable_routes_called": [],
        "report": None,
    }
    if seats_called:
        return EngineOutcome(
            accepted=True,
            disposition="accepted",
            code="accepted",
            detail="The engine recorded a seat call. This shell did not write a report.",
            engine_ref=campaign_id,
            result_json=json.dumps(summary),
            blockers=["seats_called"],
        )
    return EngineOutcome(
        accepted=False,
        disposition="intake",
        code="intake",
        detail=(
            f"Campaign {campaign_id} is bound. Seats are recorded and were not called. "
            "This is intake, not a finished crash test. The included credit was not used."
        ),
        engine_ref=campaign_id,
        result_json=json.dumps(summary),
        blockers=[],
    )


def billable_call_refused(route: str) -> EngineOutcome:
    return EngineOutcome(
        accepted=False,
        disposition="not_sent",
        code="engine_unavailable",
        detail=(
            f"{route} was not called. Team Talk has no per-customer budget the shell can set "
            "before a provider or instrument call. Intake is the only engine write this shell performs."
        ),
        blockers=["billable_route_blocked", route],
    )


class _CallError(Exception):
    def __init__(self, outcome: EngineOutcome):
        self.outcome = outcome


def _get_json(url: str):
    return _read(_request(url, method="GET"))


def _post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    return _read(_request(url, method="POST", data=data))


def _request(url: str, *, method: str, data: bytes | None = None) -> Request:
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return Request(url, data=data, method=method, headers=headers)


def _read(request: Request):
    try:
        with urlopen(request, timeout=3) as response:
            raw = response.read(64_000)
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        if exc.code in {408, 429, 500, 502, 503, 504}:
            raise _CallError(_ambiguous(f"http_{exc.code}")) from exc
        raise _CallError(_rejected(f"The engine returned HTTP {exc.code}. No model call was made by this shell.")) from exc
    except TimeoutError as exc:
        raise _CallError(_ambiguous("timeout")) from exc
    except URLError as exc:
        if _refused(exc):
            raise _CallError(
                EngineOutcome(
                    accepted=False,
                    disposition="not_sent",
                    code="engine_unavailable",
                    detail="The connection was refused before the request was sent.",
                    blockers=["connection_refused"],
                )
            ) from exc
        raise _CallError(_ambiguous("url_error")) from exc
    except OSError as exc:
        raise _CallError(_ambiguous("os_error")) from exc
    if status >= 400:
        if status in {408, 429, 500, 502, 503, 504}:
            raise _CallError(_ambiguous(f"http_{status}"))
        raise _CallError(_rejected(f"The engine returned HTTP {status}."))
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _CallError(_ambiguous("bad_json")) from exc
    return parsed


def _rejected(detail: str) -> EngineOutcome:
    return EngineOutcome(
        accepted=False,
        disposition="rejected",
        code="engine_unavailable",
        detail=detail,
        blockers=["rejected"],
    )


def _intake_is_non_model(modes) -> bool:
    if not isinstance(modes, dict) or modes.get("protocol") != "0.1.0":
        return False
    intensity = modes.get("intensity") or {}
    quick = intensity.get("QUICK") or {}
    roster = str(quick.get("roster") or "").lower()
    return "not called" in roster
