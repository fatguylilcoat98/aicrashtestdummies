"""Stub of the AICT boundary contract. No live engine calls."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from test_app import Case

TOKEN = "boundary-test-token"


def _record(status, *, error_code=None, model=False, held=False, run_id="run_stub_1"):
    return {
        "run_id": run_id,
        "owner_id": "owner",
        "status": status,
        "error_code": error_code,
        "model_invoked": model,
        "provider_invocations": 1 if model else 0,
        "reservation_held": held,
        "report_ready": status in {"completed", "failed", "uncertain"},
        "crash_result": "not_confirmed",
        "acceptance": None,
        "score": None,
        "findings_confirmed": [],
        "seats_called": False,
        "campaign_id": None,
    }


def _stub(mode):
    hits = []
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                self._send(401, {"error": "unauthorized", "model_invoked": False, "crash_result": "not_confirmed", "acceptance": None})
                return None
            owner = self.headers.get("X-AICT-Owner")
            if not owner or owner == "admin":
                self._send(400, {"error": "invalid_owner", "model_invoked": False})
                return None
            return owner

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            owner = self._auth()
            if owner is None:
                return
            if not self.path.endswith("/runs"):
                self._send(404, {"error": "not_found", "model_invoked": False})
                return
            key = self.headers.get("Idempotency-Key") or ""
            body = json.loads(raw.decode())
            hits.append((self.path, owner, key, body))
            if body.get("owner_id") != owner:
                self._send(400, {"error": "invalid_owner", "model_invoked": False})
                return
            if any(field in body for field in ("git", "zip_b64", "local_path", "instrument")):
                self._send(400, {"error": "unsupported_field", "model_invoked": False, "crash_result": "not_confirmed", "acceptance": None})
                return
            prior = seen.get((owner, key))
            if prior and prior["document"] != body.get("document_text"):
                self._send(409, {"error": "idempotency_conflict", "model_invoked": False, "run_id": prior["run_id"]})
                return
            if prior:
                self._send(200, prior["record"])
                return
            if mode == "cost_policy_absent":
                record = _record("failed", error_code="cost_policy_absent", model=False, held=False)
            elif mode == "failed":
                record = _record("failed", error_code="prepare_failed", model=False, held=False)
            elif mode == "uncertain":
                record = _record("uncertain", error_code="provider_timeout", model=True, held=True)
            elif mode == "bad":
                self._send(400, {"error": "unsupported_field", "model_invoked": False, "crash_result": "not_confirmed", "acceptance": None})
                return
            elif mode == "issues":
                record = _record("completed", error_code=None, model=True, held=True)
                record["crash_result"] = "issues_found"
            else:
                record = _record("completed", error_code=None, model=True, held=True)
            record["owner_id"] = owner
            seen[(owner, key)] = {"document": body.get("document_text"), "run_id": record["run_id"], "record": record}
            self._send(200, record)

        def do_GET(self):
            owner = self._auth()
            if owner is None:
                return
            run_id = self.path.rstrip("/").split("/")[-1]
            if run_id == "report":
                run_id = self.path.rstrip("/").split("/")[-2]
            match = None
            for (stored_owner, _key), item in seen.items():
                if item["run_id"] == run_id:
                    match = (stored_owner, item["record"])
                    break
            if match is None or match[0] != owner:
                self._send(404, {"error": "not_found", "model_invoked": False, "crash_result": "not_confirmed", "acceptance": None})
                return
            if self.path.endswith("/report"):
                if mode == "issues":
                    self._send(200, {
                        "schema": "aict.boundary.report.v2",
                        "crash_result": "issues_found",
                        "findings": [{
                            "title": "Bounds check <script>",
                            "severity": "high",
                            "evidence": "if (n < len) {",
                            "why": "The index is not checked.",
                            "suggested_fix": "Compare against the length.",
                            "verification_state": "model-found, not executed",
                            "executed": False,
                            "receipt_id": None,
                        }],
                        "findings_confirmed": [],
                        "prompt_tokens": 120,
                        "completion_tokens": 40,
                        "total_tokens": 160,
                        "spend_estimate_microusd": 424242,
                        "raw_model_text_untrusted": "not the card",
                    })
                    return
                self._send(200, {
                    "schema": "aict.boundary.report.v1",
                    "crash_result": "not_confirmed",
                    "acceptance": None,
                    "findings_confirmed": [],
                    "raw_model_text_untrusted": "This sentence is not a finding.",
                    "score": None,
                    "seats_called": False,
                })
                return
            self._send(200, match[1])

        def _send(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, hits


class BoundaryContractTests(Case):
    def _arm(self, port):
        self.app.config.engine_invocation_enabled = True
        self.app.config.engine_base_url = f"http://127.0.0.1:{port}"
        self.app.config.max_run_budget_usd = 1
        self.app.config.boundary_token = TOKEN

    def test_completed_counts_without_a_confirmed_finding(self):
        httpd, hits = _stub("success")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "What does this note support?", "boundary-key-01")
        self.assertEqual(response.status, 200, response.body)
        run = json.loads(response.body)["run"]
        self.assertEqual(run["status"], "finished_unconfirmed")
        self.assertFalse(run["has_report"])
        self.assertEqual(self.ledger(cookie)["free_committed"], 1)
        page = self.app.handle("GET", f"/app/runs/{run['id']}", {"cookie": cookie}, b"").body.decode()
        self.assertIn("not_confirmed", page)
        self.assertIn("No finding was confirmed", page)
        self.assertNotIn("This sentence is not a finding.", page.split("Untrusted")[0] if "Untrusted" in page else page)
        self.assertIn("Untrusted model text", page)
        sent = hits[0][3]
        self.assertEqual(sent["owner_id"], me["id"])
        self.assertNotIn("git", sent)
        self.assertEqual(hits[0][1], me["id"])
        httpd.shutdown()

    def test_cost_policy_absent_does_not_count(self):
        httpd, hits = _stub("cost_policy_absent")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "A note with no cost policy.", "boundary-key-02")
        run = json.loads(response.body)["run"]
        self.assertEqual(run["status"], "not_counted")
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        page = self.app.handle("GET", f"/app/runs/{run['id']}", {"cookie": cookie}, b"").body.decode()
        self.assertIn("No provider was called", page)
        self.assertEqual(len(hits), 1)
        httpd.shutdown()

    def test_failed_without_a_model_does_not_count(self):
        httpd, _hits = _stub("failed")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "Preparation failed.", "boundary-key-03")
        self.assertEqual(json.loads(response.body)["run"]["status"], "not_counted")
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        httpd.shutdown()

    def test_rejected_input_does_not_count(self):
        httpd, hits = _stub("bad")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "git: https://example.invalid/repo", "boundary-key-04")
        self.assertEqual(json.loads(response.body)["run"]["status"], "not_counted")
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        self.assertEqual(len(hits), 1)
        httpd.shutdown()

    def test_uncertain_is_held_and_not_retried(self):
        httpd, hits = _stub("uncertain")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        first = self.start_json(cookie, me["csrf"], "The reply may have been lost.", "boundary-key-05")
        self.assertEqual(first.status, 202, first.body)
        self.assertEqual(json.loads(first.body)["run"]["status"], "unknown")
        self.assertEqual(self.ledger(cookie)["free_held"], 1)
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        second = self.start_json(cookie, me["csrf"], "The reply may have been lost.", "boundary-key-05")
        self.assertTrue(json.loads(second.body)["idempotent"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(json.loads(first.body)["run"]["id"], json.loads(second.body)["run"]["id"])
        page = self.app.handle("GET", f"/app/runs/{json.loads(first.body)['run']['id']}", {"cookie": cookie}, b"").body.decode()
        self.assertIn("not retried", page.lower())
        httpd.shutdown()

    def test_replay_uses_one_post_and_other_owner_is_hidden(self):
        httpd, hits = _stub("success")
        self._arm(httpd.server_address[1])
        ada = self.signup("ada@example.com", client="ada-b")
        ada_me = self.login_api(ada)
        created = self.start_json(ada, ada_me["csrf"], "Ada's private note.", "boundary-key-06")
        run = json.loads(created.body)["run"]
        replay = self.start_json(ada, ada_me["csrf"], "Ada's private note.", "boundary-key-06")
        self.assertTrue(json.loads(replay.body)["idempotent"])
        self.assertEqual(len([item for item in hits if item[0].endswith("/runs")]), 1)
        self.assertEqual(hits[0][2][:1], "k")
        bea = self.signup("bea@example.com", client="bea-b")
        hidden = self.app.handle("GET", f"/api/runs/{run['id']}", {"cookie": bea}, b"")
        self.assertEqual(hidden.status, 404)
        self.assertNotIn("Ada's private note.", hidden.body.decode())
        from app.boundary_client import get_run
        status, body = get_run(self.app.config, owner_id=self.login_api(bea)["id"], run_id=run["engine_ref"])
        self.assertEqual(status, 404)
        self.assertNotIn("Ada", json.dumps(body))
        httpd.shutdown()

    def test_issues_found_counts_one_test_and_hides_spend_from_the_customer(self):
        from urllib.parse import urlencode
        from app.db import connect

        httpd, _hits = _stub("issues")
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "Look at this bounds check.", "boundary-key-issues")
        self.assertEqual(response.status, 200, response.body)
        run = json.loads(response.body)["run"]
        self.assertEqual(run["status"], "issues_found")
        self.assertNotIn("424242", response.body.decode())
        self.assertEqual(self.ledger(cookie)["free_committed"], 1)
        page = self.app.handle("GET", f"/app/runs/{run['id']}", {"cookie": cookie}, b"").body.decode()
        self.assertIn("Issues found", page)
        self.assertIn("Bounds check &lt;script&gt;", page)
        self.assertNotIn("Bounds check <script>", page)
        self.assertIn("if (n &lt; len) {", page)
        self.assertIn("The index is not checked.", page)
        self.assertIn("Compare against the length.", page)
        self.assertIn("model-found, not executed", page)
        self.assertIn("Not executed.", page)
        self.assertIn("No receipt.", page)
        self.assertNotIn("424242", page)
        self.assertNotIn("microusd", page)
        stored = connect(self.app.config.db_path).execute(
            "SELECT prompt_tokens, completion_tokens, total_tokens, spend_estimate_microusd FROM runs WHERE id = ?",
            (run["id"],),
        ).fetchone()
        self.assertEqual(stored["prompt_tokens"], 120)
        self.assertEqual(stored["completion_tokens"], 40)
        self.assertEqual(stored["total_tokens"], 160)
        self.assertEqual(stored["spend_estimate_microusd"], 424242)
        self.app.config.operator_token = "operator-token-for-tests"
        denied = self.app.handle("GET", "/admin/visitors", {"cookie": cookie}, b"")
        self.assertEqual(denied.status, 401)
        login = self.app.handle(
            "POST",
            "/admin/operator-login",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"token": "operator-token-for-tests"}).encode(),
        )
        operator = [value.split(";", 1)[0] for key, value in login.headers if key.lower() == "set-cookie" and value.startswith("actd_operator=")][0]
        board = self.app.handle("GET", "/admin/visitors", {"cookie": operator}, b"").body.decode()
        self.assertIn("424242", board)
        self.assertIn("microusd", board)
        httpd.shutdown()

    def test_boundary_timeout_defaults_to_120_seconds(self):
        import time
        from app.boundary_client import _Transport, _json_request
        from app.config import Config

        self.assertEqual(Config().boundary_timeout_seconds, 120)

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.4)
                raw = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            with self.assertRaises(_Transport) as caught:
                _json_request(
                    f"http://127.0.0.1:{port}/runs",
                    method="GET",
                    payload=None,
                    token="t",
                    owner_id="owner",
                    idempotency_key=None,
                    timeout=0.05,
                )
            self.assertEqual(caught.exception.outcome.disposition, "ambiguous")
            self.assertIn("timeout", caught.exception.outcome.blockers or [])
        finally:
            httpd.shutdown()
