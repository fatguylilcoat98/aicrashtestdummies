"""Auth, quota, payment, isolation, and engine-gate tests."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from app import payments
from app.config import Config, FREE_CREDITS
from app.db import connect
from app.server import App, assert_no_seat_names, make_server

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.id().replace(".", "_"))
        # Unique directory under /tmp so tests do not share a database.
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.app = self.make_app()

    def tearDown(self):
        self._dir.cleanup()

    def make_app(self, **overrides) -> App:
        data = {
            "env": "test",
            "data_dir": self.tmp / "data",
            "public_root": ROOT,
            "secret": "test-secret-value",
            "free_credits": FREE_CREDITS,
            "auth_limit": 40,
            "start_limit": 40,
            "cookie_secure": False,
        }
        data.update(overrides)
        return App(Config(**data))

    def form_pair(self, path, app=None):
        app = app or self.app
        page = app.handle("GET", path, {}, b"")
        self.assertEqual(page.status, 200, page.body[:200])
        form_cookie = ""
        for key, value in page.headers:
            if key.lower() == "set-cookie" and value.startswith("actd_form="):
                form_cookie = value.split(";", 1)[0]
        import re
        match = re.search(r'name="csrf" value="([^"]+)"', page.body.decode())
        self.assertIsNotNone(match)
        return form_cookie, match.group(1)

    def signup(self, email="ada@example.com", client="signup", password=PASSWORD, app=None):
        app = app or self.app
        form_cookie, token = self.form_pair("/account/signup", app)
        body = urlencode({"email": email, "password": password, "confirm": password, "csrf": token}).encode()
        response = app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded", "cookie": form_cookie},
            body,
            client=client,
        )
        self.assertEqual(response.status, 303, response.body)
        return self.cookie(response)

    def login(self, email, password, client="login", app=None):
        app = app or self.app
        form_cookie, token = self.form_pair("/account/login", app)
        body = urlencode({"email": email, "password": password, "csrf": token}).encode()
        return app.handle(
            "POST",
            "/account/login",
            {"content-type": "application/x-www-form-urlencoded", "cookie": form_cookie},
            body,
            client=client,
        )

    def login_api(self, cookie, app=None):
        app = app or self.app
        me = app.handle("GET", "/api/me", {"cookie": cookie}, b"")
        self.assertEqual(me.status, 200, me.body)
        return json.loads(me.body)

    def cookie(self, response) -> str:
        for key, value in response.headers:
            if key.lower() == "set-cookie":
                return value.split(";", 1)[0]
        self.fail("missing session cookie")

    def auth_headers(self, cookie, csrf, *, json_body=True):
        headers = {"cookie": cookie, "x-csrf-token": csrf}
        if json_body:
            headers["content-type"] = "application/json"
        return headers

    def start_json(self, cookie, csrf, question, key, app=None, client="start"):
        app = app or self.app
        body = json.dumps({
            "question": question,
            "idempotency_key": key,
            "privacy": "yes",
            "budget_usd": 100000,
            "credits": 99,
        }).encode()
        return app.handle("POST", "/api/runs", self.auth_headers(cookie, csrf), body, client=client)

    def ledger(self, cookie, app=None):
        app = app or self.app
        response = app.handle("GET", "/api/ledger", {"cookie": cookie}, b"")
        self.assertEqual(response.status, 200, response.body)
        return json.loads(response.body)


class AuthTests(Case):
    def test_signup_login_logout_and_session_rotation(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        self.assertEqual(me["email"], "ada@example.com")
        self.assertNotIn("password", json.dumps(me).lower())
        page = self.app.handle("GET", "/app", {"cookie": cookie}, b"")
        self.assertEqual(page.status, 200)
        self.assertIn("5", page.body.decode())
        self.assertIn("of 5", page.body.decode())
        logged_out = self.app.handle(
            "POST",
            "/account/logout",
            {"cookie": cookie, "content-type": "application/x-www-form-urlencoded"},
            urlencode({"csrf": me["csrf"]}).encode(),
        )
        self.assertEqual(logged_out.status, 303)
        again = self.app.handle("GET", "/api/me", {"cookie": cookie}, b"")
        self.assertEqual(again.status, 401)
        bad = self.login("ada@example.com", "wrong-password-here", client="login-bad")
        self.assertEqual(bad.status, 401)
        self.assertNotIn(PASSWORD, bad.body.decode())
        good = self.login("Ada@Example.com", PASSWORD, client="login-good")
        self.assertEqual(good.status, 303)
        self.assertNotEqual(self.cookie(good), cookie)

    def test_duplicate_signup_and_short_password(self):
        self.signup(client="a")
        form_cookie, token = self.form_pair("/account/signup")
        again = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded", "cookie": form_cookie},
            urlencode({"email": "ada@example.com", "password": PASSWORD, "confirm": PASSWORD, "csrf": token}).encode(),
            client="b",
        )
        self.assertEqual(again.status, 409)
        form_cookie, token = self.form_pair("/account/signup")
        short = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded", "cookie": form_cookie},
            urlencode({"email": "bea@example.com", "password": "short", "confirm": "short", "csrf": token}).encode(),
            client="c",
        )
        self.assertEqual(short.status, 400)
        self.assertIsNone(connect(self.app.config.db_path).execute("SELECT id FROM users WHERE email = 'bea@example.com'").fetchone())

    def test_allowance_cannot_be_raised_from_the_environment(self):
        previous = os.environ.get("ACTD_FREE_CREDITS"), os.environ.get("ACTD_SESSION_SECRET"), os.environ.get("ACTD_ENV")
        os.environ["ACTD_FREE_CREDITS"] = "99"
        os.environ["ACTD_SESSION_SECRET"] = "unit-test-secret"
        os.environ["ACTD_ENV"] = "development"
        try:
            config = Config.from_env()
        finally:
            for name, value in zip(("ACTD_FREE_CREDITS", "ACTD_SESSION_SECRET", "ACTD_ENV"), previous):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(config.free_credits, 5)

    def test_admin_is_not_a_public_registration_flag(self):
        self.app.config.admin_emails = frozenset({"ada@example.com"})
        cookie = self.signup()
        me = self.login_api(cookie)
        self.assertEqual(me["role"], "user")
        self.assertEqual(self.app.handle("GET", "/admin", {"cookie": cookie}, b"").status, 404)
        self.app.config.operator_token = "operator-token-for-tests"
        denied = self.app.handle(
            "POST",
            "/api/operator/promote",
            {"content-type": "application/json", "authorization": "Bearer wrong-token"},
            json.dumps({"email": "ada@example.com"}).encode(),
        )
        self.assertEqual(denied.status, 404)
        self.assertEqual(self.login_api(cookie)["role"], "user")
        promoted = self.app.handle(
            "POST",
            "/api/operator/promote",
            {"content-type": "application/json", "authorization": "Bearer operator-token-for-tests"},
            json.dumps({"email": "ada@example.com"}).encode(),
        )
        self.assertEqual(promoted.status, 200, promoted.body)
        self.assertEqual(self.login_api(cookie)["role"], "admin")
        page = self.app.handle("GET", "/admin", {"cookie": cookie}, b"")
        self.assertEqual(page.status, 200)
        self.assertIn("Requests are not people", page.body.decode())
        self.assertIn("Model cost policy", page.body.decode())
        self.assertIn("missing", page.body.decode())
        self.assertIn("not an account", page.body.decode().lower())
        other = self.signup("bea@example.com", client="bea")
        hidden = self.app.handle("GET", "/admin", {"cookie": other}, b"")
        self.assertEqual(hidden.status, 404)
        metrics = self.app.handle("GET", "/api/admin/metrics", {"cookie": other}, b"")
        self.assertEqual(metrics.status, 404)
        self.assertNotIn("ada@example.com", metrics.body.decode())


class QuotaTests(Case):
    def test_engine_unavailable_releases_and_retry_does_not_charge_twice(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        first = self.start_json(cookie, me["csrf"], "Can an ordinary holder reach withdraw()?", "attempt-key-01")
        self.assertEqual(first.status, 200, first.body)
        payload = json.loads(first.body)
        self.assertEqual(payload["run"]["status"], "engine_unavailable")
        self.assertIn("invocation_disabled", payload["engine_blockers"])
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        second = self.start_json(cookie, me["csrf"], "Can an ordinary holder reach withdraw()?", "attempt-key-01")
        self.assertEqual(second.status, 200)
        self.assertTrue(json.loads(second.body)["idempotent"])
        self.assertEqual(json.loads(second.body)["run"]["id"], payload["run"]["id"])
        runs = json.loads(self.app.handle("GET", "/api/runs", {"cookie": cookie}, b"").body)
        self.assertEqual(len(runs["runs"]), 1)
        self.assertEqual(self.ledger(cookie)["remaining"], 5)

    def test_six_concurrent_starts_cannot_hold_six_credits(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        release = threading.Event()
        started = []
        results = []
        lock = threading.Lock()

        def hold(info):
            with lock:
                started.append(info["run_id"])
            self.assertTrue(release.wait(5))

        self.app.config.before_engine = hold

        def worker(index):
            response = self.start_json(cookie, me["csrf"], f"Question {index} for the race.", f"race-key-{index}")
            with lock:
                results.append(response)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if len(started) >= 5 and any(item.status == 402 for item in results):
                    break
            time.sleep(0.02)
        with lock:
            self.assertEqual(len(started), 5, results)
            self.assertEqual(sum(1 for item in results if item.status == 402), 1)
        release.set()
        for thread in threads:
            thread.join(5)
        statuses = sorted(item.status for item in results)
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 402])
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        count = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        self.assertEqual(count, 5)

    def test_crash_before_acceptance_is_recovered_without_losing_the_credit(self):
        cookie = self.signup()
        me = self.login_api(cookie)

        def boom(_info):
            raise RuntimeError("worker crashed")

        self.app.config.before_engine = boom
        failed = self.start_json(cookie, me["csrf"], "This attempt crashes after the hold.", "crash-key-01")
        self.assertEqual(failed.status, 500)
        self.assertNotIn("This attempt crashes", failed.body.decode())
        held = self.ledger(cookie)
        self.assertEqual(held["free_held"], 1)
        self.assertEqual(held["free_remaining"], 4)
        conn = connect(self.app.config.db_path)
        conn.execute("UPDATE reservations SET created_at = ? WHERE state = 'reserved'", ("2000-01-01T00:00:00Z",))
        conn.close()
        result = self.app.recover()
        self.assertEqual(result["released_unsent"], 1)
        self.assertEqual(result["unknown"], 0)
        restored = self.ledger(cookie)
        self.assertEqual(restored["free_held"], 0)
        self.assertEqual(restored["free_remaining"], 5)
        self.assertTrue(any("never left this server" in entry["note"] for entry in restored["entries"]))

    def test_cancel_before_the_engine_releases_once(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        gate = threading.Event()
        seen = threading.Event()
        box = {}

        def hold(info):
            box.update(info)
            seen.set()
            gate.wait(5)

        self.app.config.before_engine = hold
        result = {}

        def worker():
            result["response"] = self.start_json(cookie, me["csrf"], "Cancel me before acceptance.", "cancel-key-01")

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(seen.wait(5))
        cancelled = self.app.handle(
            "POST",
            f"/api/runs/{box['run_id']}/cancel",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertEqual(cancelled.status, 200, cancelled.body)
        gate.set()
        thread.join(5)
        body = json.loads(cancelled.body)
        self.assertEqual(body["run"]["status"], "cancelled")
        self.assertTrue(body["released"])
        again = self.app.handle(
            "POST",
            f"/api/runs/{box['run_id']}/cancel",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertFalse(json.loads(again.body)["released"])
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)


class EngineTests(Case):
    def test_disabled_engine_is_not_called_and_direct_budget_is_ignored(self):
        hits = []
        httpd = _engine_server(hits, 500, b"{}")
        port = httpd.server_address[1]
        self._arm(port)
        self.app.config.max_run_budget_usd = None
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "Do not call the model.", "budget-key-01")
        self.assertEqual(response.status, 200)
        self.assertEqual(hits, [])
        self.assertIn("budget_missing", json.loads(response.body)["engine_blockers"])
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        httpd.shutdown()

    def test_engine_error_does_not_commit(self):
        hits = []
        httpd = _engine_server(hits, 500, b'{"id":"nope"}')
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "A failing engine must not bill.", "error-key-01")
        self.assertEqual(json.loads(response.body)["run"]["status"], "unknown")
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        self.assertEqual(self.ledger(cookie)["free_held"], 1)
        self.assertTrue(any(item[0] == "/api/aict/v1/runs" for item in hits))
        httpd.shutdown()

    def test_public_tunnel_is_not_posted(self):
        from app.boundary_client import BILLABLE_ROUTES

        self.app.config.engine_invocation_enabled = True
        self.app.config.engine_base_url = "https://teamtalk.thegoodneighborguard.com"
        self.app.config.max_run_budget_usd = 1
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "Do not open a public campaign.", "tunnel-key-01")
        body = json.loads(response.body)
        self.assertEqual(body["run"]["status"], "engine_unavailable")
        self.assertIn("public_tunnel_blocked", body["engine_blockers"])
        self.assertEqual(self.ledger(cookie)["free_remaining"], 5)
        self.assertTrue(BILLABLE_ROUTES)

    def _arm(self, port: int):
        contract = self.tmp / "contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "actd.engine-binding/1",
                    "start": {"method": "POST", "path": "/v1/investigations"},
                    "request_fields": ["question", "run_id"],
                    "accept_field": "id",
                    "budget": {"enforced_by": "engine"},
                }
            ),
            encoding="utf-8",
        )
        self.app.config.boundary_token = "boundary-test-token"
        self.app.config.contract_path = contract
        self.app.config.engine_base_url = f"http://127.0.0.1:{port}"
        self.app.config.engine_start_path = "/v1/investigations"
        self.app.config.engine_invocation_enabled = True
        self.app.config.max_run_budget_usd = 1.5


class PaymentTests(Case):
    def test_checkout_is_fail_closed_without_a_price(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        billing = self.app.handle("GET", "/app/billing", {"cookie": cookie}, b"")
        text = billing.body.decode()
        self.assertIn("Checkout unavailable", text)
        self.assertNotIn("$", text)
        self.assertNotIn("14,000", text)
        self.assertNotIn("14000", text)
        checkout = self.app.handle(
            "POST",
            "/api/billing/checkout",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertEqual(checkout.status, 403)
        body = json.loads(checkout.body)
        self.assertEqual(body["error"], "payments_disabled")
        self.assertIsNone(body["price"])
        self.assertFalse(body["collects_card_numbers"])
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)

    def test_live_price_without_a_model_cost_policy_stays_closed(self):
        self.app.config.payments_mode = "live"
        self.app.config.allow_live_charges = True
        self.app.config.payment_provider = "stripe"
        self.app.config.payment_webhook_secret = "live-webhook-secret"
        self.app.config.price_cents = 2500
        self.app.config.price_currency = "USD"
        self.app.config.credit_pack = 5
        self.app.config.env = "development"
        cookie = self.signup()
        me = self.login_api(cookie)
        billing = self.app.handle("GET", "/app/billing", {"cookie": cookie}, b"").body.decode()
        self.assertIn("no model rate card", billing)
        self.assertNotIn("2500", billing)
        self.assertNotIn("$", billing)
        checkout = self.app.handle(
            "POST",
            "/api/billing/checkout",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertEqual(checkout.status, 403)
        self.assertIsNone(json.loads(checkout.body)["price"])
        health = json.loads(self.app.handle("GET", "/api/health", {}, b"").body)
        self.assertEqual(health["model_cost_policy"], "missing")
        self.assertIsNone(health["customer_price"])

    def test_webhook_signature_is_a_test_double_and_does_not_grant_credits(self):
        self._enable_fixture()
        cookie = self.signup()
        me = self.login_api(cookie)
        checkout = self.app.handle(
            "POST",
            "/api/billing/checkout",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertEqual(checkout.status, 403, checkout.body)
        self.assertEqual(json.loads(checkout.body)["provider"], None)
        secret = "fixture-webhook-secret"
        good = self._event(me["id"], "evt_1", 5)
        self.assertTrue(payments.verify_signature(secret, self._signed(secret, good)[0][payments.SIGNATURE_HEADER], good, int(time.time())))
        self.assertFalse(payments.verify_signature(secret, "t=1,v1=deadbeef", good, int(time.time())))
        granted = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, good)[0], good)
        self.assertEqual(granted.status, 403)
        self.assertEqual(json.loads(granted.body)["integration"], "none")
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)
        replay = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, good)[0], good)
        self.assertEqual(replay.status, 403)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)

    def test_disabled_mode_ignores_a_valid_signature(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        secret = "not-enabled"
        self.app.config.payment_webhook_secret = secret
        body = self._event(me["id"], "evt_disabled", 5)
        response = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, body)[0], body)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)

    def test_paid_credit_is_required_after_the_allowance_and_commits_once(self):
        self.app = self.make_app(free_credits=0)
        self._enable_fixture()
        from app.engine import EngineOutcome

        def transport(run_id, question):
            return EngineOutcome(
                accepted=True,
                disposition="accepted",
                code="finished_unconfirmed",
                detail="A provider call finished. No finding was confirmed.",
                engine_ref="boundary-run-1",
                result_json=json.dumps({
                    "status": "completed",
                    "crash_result": "not_confirmed",
                    "acceptance": None,
                    "findings_confirmed": [],
                    "model_invoked": True,
                }),
            )

        self.app.config.engine_invocation_enabled = True
        self.app.config.engine_base_url = "http://127.0.0.1:5001"
        self.app.config.max_run_budget_usd = 1
        self.app.config.boundary_token = "boundary-test-token"
        self.app.config.engine_transport = transport
        cookie = self.signup(app=self.app)
        me = self.login_api(cookie, app=self.app)
        blocked = self.start_json(cookie, me["csrf"], "Needs a paid credit.", "paid-key-0001", app=self.app)
        self.assertEqual(blocked.status, 402)
        body = self._event(me["id"], "evt_pack", 5)
        ignored = self.app.handle("POST", "/api/billing/webhook", self._signed("fixture-webhook-secret", body)[0], body)
        self.assertEqual(ignored.status, 403)
        self.assertEqual(self.ledger(cookie, app=self.app)["paid_credits"], 0)
        from app.auth import iso, utcnow
        from app.ledger import grant_paid
        grant_paid(connect(self.app.config.db_path), me["id"], 5, "operator-grant-1", "hash", iso(utcnow()))
        accepted = self.start_json(cookie, me["csrf"], "Needs a paid credit.", "paid-key-0001", app=self.app)
        self.assertEqual(json.loads(accepted.body)["run"]["status"], "finished_unconfirmed")
        self.assertFalse(json.loads(accepted.body)["run"]["has_report"])
        self.assertEqual(self.ledger(cookie, app=self.app)["paid_committed"], 1)
        self.assertEqual(self.ledger(cookie, app=self.app)["paid_remaining"], 4)

    def _enable_fixture(self):
        self.app.config.payments_mode = "fixture"
        self.app.config.payment_provider = "fixture"
        self.app.config.payment_webhook_secret = "fixture-webhook-secret"
        self.app.config.price_cents = 100
        self.app.config.price_currency = "USD"
        self.app.config.credit_pack = 5
        self.app.config.env = "development"

    def _arm_on(self, app, port):
        contract = self.tmp / "contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "actd.engine-binding/1",
                    "start": {"method": "POST", "path": "/v1/investigations"},
                    "request_fields": ["question", "run_id"],
                    "accept_field": "id",
                    "budget": {"enforced_by": "engine"},
                }
            ),
            encoding="utf-8",
        )
        app.config.contract_path = contract
        app.config.engine_base_url = f"http://127.0.0.1:{port}"
        app.config.engine_start_path = "/v1/investigations"
        app.config.engine_invocation_enabled = True
        app.config.max_run_budget_usd = 1.5

    def _event(self, user_id, event_id, credits) -> bytes:
        return json.dumps(
            {"id": event_id, "type": "checkout.completed", "user_id": user_id, "credits": credits}
        ).encode()

    def _signed(self, secret, body):
        stamp = int(time.time())
        headers = {
            "content-type": "application/json",
            payments.SIGNATURE_HEADER: payments.header_value(stamp, payments.sign(secret, stamp, body)),
        }
        return headers, body


class SecurityTests(Case):
    def test_owner_isolation_and_direct_api_bypass(self):
        ada = self.signup("ada@example.com", client="ada")
        ada_me = self.login_api(ada)
        created = self.start_json(ada, ada_me["csrf"], "SECRET-QUESTION-ALPHA <script>", "ada-key-01")
        run_id = json.loads(created.body)["run"]["id"]
        bea = self.signup("bea@example.com", client="bea")
        foreign = self.app.handle("GET", f"/api/runs/{run_id}", {"cookie": bea}, b"")
        self.assertEqual(foreign.status, 404)
        self.assertNotIn("SECRET-QUESTION-ALPHA", foreign.body.decode())
        page = self.app.handle("GET", f"/app/runs/{run_id}", {"cookie": bea}, b"")
        self.assertEqual(page.status, 404)
        self.assertNotIn("SECRET-QUESTION-ALPHA", page.body.decode())
        own = self.app.handle("GET", f"/app/runs/{run_id}", {"cookie": ada}, b"")
        self.assertIn("&lt;script&gt;", own.body.decode())
        self.assertNotIn("<script>", own.body.decode())
        before = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        bypass = self.app.handle(
            "POST",
            "/api/runs",
            {"content-type": "application/json"},
            json.dumps({"question": "Bypass the cookie.", "idempotency_key": "bypass-key-01"}).encode(),
        )
        self.assertEqual(bypass.status, 401)
        missing_csrf = self.app.handle(
            "POST",
            "/api/runs",
            {"cookie": ada, "content-type": "application/json"},
            json.dumps({"question": "Missing the CSRF token.", "idempotency_key": "csrf-key-0001"}).encode(),
        )
        self.assertEqual(missing_csrf.status, 403)
        after = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        self.assertEqual(before, after)
        upload = self.app.handle(
            "POST",
            "/app/runs",
            {"cookie": ada, "content-type": "multipart/form-data; boundary=x"},
            b"not-a-file",
        )
        self.assertEqual(upload.status, 400)
        self.assertFalse(list((self.tmp / "data").glob("*.zip")))
        self.assertEqual(after, connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"])

    def test_rate_limit_and_unknown_paths(self):
        self.app.config.auth_limit = 2
        for index in range(2):
            self.app.handle(
                "POST",
                "/account/login",
                {"content-type": "application/x-www-form-urlencoded"},
                urlencode({"email": "ada@example.com", "password": PASSWORD}).encode(),
                client="same-network",
            )
        blocked = self.app.handle(
            "POST",
            "/account/login",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"email": "ada@example.com", "password": PASSWORD}).encode(),
            client="same-network",
        )
        self.assertEqual(blocked.status, 429)
        missing = self.app.handle("GET", "/no/such/page", {}, b"")
        self.assertEqual(missing.status, 404)
        text = missing.body.decode()
        self.assertIn("Page not found", text)
        self.assertNotIn("We don't agree", text)
        identity = self.app.handle("GET", "/engine/identity-map.json", {}, b"")
        self.assertEqual(identity.status, 404)
        self.assertFalse(assert_no_seat_names(identity.body.decode()) is False and "deepseek" in identity.body.decode())
        self.assertNotIn("deepseek", identity.body.decode())
        traversal = self.app.handle("GET", "/assets/../engine/identity-map.json", {}, b"")
        self.assertEqual(traversal.status, 404)
        self.assertNotIn("deepseek", traversal.body.decode())
        robots = self.app.handle("GET", "/robots.txt", {}, b"").body.decode()
        self.assertIn("Disallow: /engine", robots)
        self.assertIn("Disallow: /api", robots)
        sitemap = self.app.handle("GET", "/sitemap.xml", {}, b"").body.decode()
        self.assertIn("/pricing.html", sitemap)
        self.assertIn("https://aicrashtestdummies.com/account/signup", sitemap)
        self.assertNotIn("app.aicrashtestdummies.com", sitemap)
        self.assertNotIn("/app/billing", sitemap)
        health = json.loads(self.app.handle("GET", "/api/health", {}, b"").body)
        self.assertIsNone(health["historic_visitors"])
        self.assertEqual(health["engine"], "unavailable")
        self.assertIn("team_talk_absent", health["engine_blockers"])
        self.assertIn("team_talk_absent", health["engine_blockers"])
        self.assertNotIn("contract_missing", health["engine_blockers"])
        self.assertEqual(health["payments"], "disabled")
        self.assertEqual(health["model_cost_policy"], "missing")
        self.assertIsNone(health["customer_price"])
        home = self.app.handle("GET", "/", {}, b"").body.decode()
        self.assertIn("We don't agree", home)
        self.assertNotIn("Early User", home)
        self.assertTrue(assert_no_seat_names(home))

    def test_http_cookie_roundtrip_and_metrics_do_not_count_requests_as_people(self):
        httpd = make_server(self.app, port=0)
        host, port = httpd.server_address
        try:
            status, headers, body = _http(port, "GET", "/account/signup", None, {})
            self.assertEqual(status, 200, body)
            import re
            token = re.search(br'name="csrf" value="([^"]+)"', body).group(1).decode()
            form_cookie = headers["set-cookie"].split(";", 1)[0]
            status, headers, body = _http(
                port,
                "POST",
                "/account/signup",
                urlencode({"email": "ada@example.com", "password": PASSWORD, "confirm": PASSWORD, "csrf": token}).encode(),
                {"content-type": "application/x-www-form-urlencoded", "cookie": form_cookie},
            )
            self.assertEqual(status, 303, body)
            cookie = headers["set-cookie"].split(";", 1)[0]
            status, _, body = _http(port, "GET", "/app", b"", {"cookie": cookie})
            self.assertEqual(status, 200)
            self.assertIn(b"Included", body)
            status, _, body = _http(port, "GET", "/api/admin/metrics", b"", {"cookie": cookie})
            self.assertEqual(status, 404)
        finally:
            httpd.shutdown()
        self.app.config.operator_token = "operator-token-for-tests"
        cookie = self.cookie(self.login("ada@example.com", PASSWORD, client="metrics"))
        promoted = self.app.handle(
            "POST",
            "/api/operator/promote",
            {"authorization": "Bearer operator-token-for-tests", "content-type": "application/json"},
            json.dumps({"email": "ada@example.com"}).encode(),
        )
        self.assertEqual(promoted.status, 200, promoted.body)
        admin = json.loads(self.app.handle("GET", "/api/admin/metrics", {"cookie": cookie}, b"").body)
        self.assertGreaterEqual(admin["anonymous_requests"], 1)
        self.assertGreaterEqual(admin["authenticated_requests"], 1)
        self.assertGreaterEqual(admin["unique_signed_in_accounts"], 1)
        self.assertIsNone(admin["historic_visitors"])
        self.assertLess(admin["unique_signed_in_accounts"], admin["anonymous_requests"] + admin["authenticated_requests"])


        self.assertLess(admin["unique_signed_in_accounts"], admin["anonymous_requests"] + admin["authenticated_requests"])


class HardeningTests(Case):
    def test_signup_login_and_logout_require_csrf_and_sessions_can_be_revoked(self):
        bare = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"email": "ada@example.com", "password": PASSWORD, "confirm": PASSWORD}).encode(),
        )
        self.assertEqual(bare.status, 403)
        self.assertIsNone(connect(self.app.config.db_path).execute("SELECT id FROM users").fetchone())
        cookie = self.signup()
        me = self.login_api(cookie)
        kept = self.app.handle(
            "POST",
            "/account/logout",
            {"cookie": cookie, "content-type": "application/x-www-form-urlencoded"},
            b"",
        )
        self.assertEqual(kept.status, 403)
        self.assertEqual(self.login_api(cookie)["email"], "ada@example.com")
        form_cookie, token = self.form_pair("/account/login")
        fixed = self.app.handle(
            "POST",
            "/account/login",
            {"content-type": "application/x-www-form-urlencoded", "cookie": f"{form_cookie}; actd_session=attacker-fixed-token"},
            urlencode({"email": "ada@example.com", "password": PASSWORD, "csrf": token}).encode(),
            client="fix",
        )
        self.assertEqual(fixed.status, 303)
        issued = self.cookie(fixed)
        self.assertNotIn("attacker-fixed-token", issued)
        stale = self.app.handle("GET", "/api/me", {"cookie": "actd_session=attacker-fixed-token"}, b"")
        self.assertEqual(stale.status, 401)
        self.assertEqual(self.login_api(cookie)["email"], "ada@example.com")
        second = self.cookie(self.login("ada@example.com", PASSWORD, client="second"))
        self.assertEqual(self.login_api(second)["email"], "ada@example.com")
        revoked = self.app.handle(
            "POST",
            "/api/sessions/revoke",
            {"cookie": second, "content-type": "application/json", "x-csrf-token": self.login_api(second)["csrf"]},
            b"{}",
        )
        self.assertEqual(revoked.status, 200)
        self.assertEqual(self.app.handle("GET", "/api/me", {"cookie": cookie}, b"").status, 401)
        self.assertEqual(self.app.handle("GET", "/api/me", {"cookie": second}, b"").status, 401)

    def test_forwarded_for_and_extra_accounts_do_not_mint_admin_or_bypass_limits(self):
        self.app.config.auth_limit = 2
        self.login("ada@example.com", "wrong-password-here", client="203.0.113.10")
        self.login("ada@example.com", "wrong-password-here", client="203.0.113.11")
        form_cookie, token = self.form_pair("/account/login")
        blocked = self.app.handle(
            "POST",
            "/account/login",
            {
                "content-type": "application/x-www-form-urlencoded",
                "x-forwarded-for": "198.51.100.20",
                "cookie": form_cookie,
            },
            urlencode({"email": "ada@example.com", "password": "wrong-password-here", "csrf": token}).encode(),
            client="203.0.113.12",
        )
        self.assertEqual(blocked.status, 429)
        self.app.config.auth_limit = 30
        self.app.config.signup_limit = 2
        self.signup("one@example.com", client="203.0.113.10")
        self.signup("two@example.com", client="203.0.113.10")
        third = self.signup("three@example.com", client="203.0.113.8")
        self.assertTrue(third)
        capped = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded", "x-forwarded-for": "198.51.100.9"},
            urlencode({"email": "four@example.com", "password": PASSWORD, "confirm": PASSWORD}).encode(),
            client="203.0.113.10",
        )
        self.assertEqual(capped.status, 429)
        self.assertIsNone(connect(self.app.config.db_path).execute("SELECT id FROM users WHERE email = 'four@example.com'").fetchone())

    def test_email_guessing_cannot_become_admin_and_unverified_production_cannot_spend(self):
        previous = {key: os.environ.get(key) for key in ("ACTD_ENV", "ACTD_SESSION_SECRET", "ACTD_ADMIN_EMAILS", "ACTD_DATA_DIR", "ACTD_COOKIE_SECURE")}
        os.environ["ACTD_ENV"] = "production"
        os.environ["ACTD_SESSION_SECRET"] = "production-test-secret"
        os.environ["ACTD_ADMIN_EMAILS"] = "ada@example.com"
        os.environ["ACTD_DATA_DIR"] = str(self.tmp / "prod")
        os.environ["ACTD_COOKIE_SECURE"] = "0"
        try:
            app = App(Config.from_env())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertTrue(app.config.require_verified_email)
        cookie = self.signup("ada@example.com", client="prod", app=app)
        me = self.login_api(cookie, app=app)
        self.assertEqual(me["role"], "user")
        self.assertEqual(app.handle("GET", "/admin", {"cookie": cookie}, b"").status, 404)
        self.assertEqual(self.ledger(cookie, app=app)["credits_locked"], True)
        self.assertEqual(self.ledger(cookie, app=app)["free_remaining"], 0)
        blocked = self.start_json(cookie, me["csrf"], "Locked until the address is verified.", "prod-key-01", app=app)
        self.assertEqual(blocked.status, 403)
        self.assertEqual(json.loads(blocked.body)["error"], "verification_required")
        self.assertEqual(connect(app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"], 0)
        health = json.loads(app.handle("GET", "/api/health", {}, b"").body)
        self.assertFalse(health["public_launch"])
        self.assertEqual(health["email_verification"], "missing")
        self.assertFalse(health["admin_from_unverified_email"])
        same = app.handle("POST", "/account/recover", {"content-type": "application/x-www-form-urlencoded"}, urlencode({"email": "ada@example.com"}).encode()).body
        missing = app.handle("POST", "/account/recover", {"content-type": "application/x-www-form-urlencoded"}, urlencode({"email": "nobody@example.com"}).encode()).body
        self.assertEqual(same, missing)
        self.assertIn(b"does not change a password", same)
        still = self.login("ada@example.com", PASSWORD, client="still", app=app)
        self.assertEqual(still.status, 303)

    def test_lost_engine_reply_stays_held_across_retry_and_restart(self):
        calls = {"n": 0}

        def transport(run_id, question):
            calls["n"] += 1
            from app.engine import EngineOutcome
            return EngineOutcome(accepted=False, disposition="ambiguous", code="unknown", detail="Reply lost.")

        self.app = self.make_app(free_credits=1)
        self._arm(self.app)
        self.app.config.engine_transport = transport
        cookie = self.signup(app=self.app)
        me = self.login_api(cookie, app=self.app)
        first = self.start_json(cookie, me["csrf"], "Did the call land?", "unknown-key-01", app=self.app)
        self.assertEqual(first.status, 202, first.body)
        self.assertEqual(json.loads(first.body)["run"]["status"], "unknown")
        self.assertEqual(self.ledger(cookie, app=self.app)["free_held"], 1)
        self.assertEqual(self.ledger(cookie, app=self.app)["free_committed"], 0)
        again = self.start_json(cookie, me["csrf"], "Did the call land?", "unknown-key-01", app=self.app)
        self.assertTrue(json.loads(again.body)["idempotent"])
        self.assertEqual(calls["n"], 1)
        blocked = self.start_json(cookie, me["csrf"], "A second call must not start.", "unknown-key-02", app=self.app)
        self.assertEqual(blocked.status, 402)
        self.assertEqual(calls["n"], 1)
        run_id = json.loads(first.body)["run"]["id"]
        owner = self.app.handle(
            "POST",
            f"/api/operator/runs/{run_id}/resolve",
            {"cookie": cookie, "content-type": "application/json", "x-csrf-token": me["csrf"]},
            json.dumps({"action": "release"}).encode(),
        )
        self.assertEqual(owner.status, 404)
        self.assertEqual(self.ledger(cookie, app=self.app)["free_held"], 1)
        self.app.config.operator_token = "operator-token-for-tests"
        released = self.app.handle(
            "POST",
            f"/api/operator/runs/{run_id}/resolve",
            {"authorization": "Bearer operator-token-for-tests", "content-type": "application/json"},
            json.dumps({"action": "release"}).encode(),
        )
        self.assertEqual(released.status, 200, released.body)
        self.assertEqual(json.loads(released.body)["run"]["status"], "released")
        self.assertIsNone(json.loads(released.body)["run"]["engine_ref"])
        self.assertEqual(self.ledger(cookie, app=self.app)["free_remaining"], 1)

        def boom(_info):
            raise RuntimeError("died after send started")

        self.app.config.after_dispatch = boom
        self.app.config.engine_transport = transport
        failed = self.start_json(cookie, me["csrf"], "Restart in the middle.", "restart-key-01", app=self.app)
        self.assertEqual(failed.status, 500)
        self.assertEqual(self.ledger(cookie, app=self.app)["free_held"], 1)
        restarted = self.app.recover()
        self.assertEqual(restarted["unknown"], 1)
        self.assertEqual(restarted["released_unsent"], 0)
        self.assertEqual(self.ledger(cookie, app=self.app)["free_remaining"], 0)
        conn = connect(self.app.config.db_path)
        conn.execute("UPDATE reservations SET created_at = ?, updated_at = ? WHERE state = 'unknown'", ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))
        conn.close()
        again_recover = self.app.recover()
        self.assertEqual(again_recover["released_unsent"], 0)
        self.assertEqual(self.ledger(cookie, app=self.app)["free_held"], 1)
        run = connect(self.app.config.db_path).execute("SELECT id, status FROM runs WHERE status = 'unknown'").fetchone()
        cancelled = self.app.handle(
            "POST",
            f"/api/runs/{run['id']}/cancel",
            self.auth_headers(cookie, me["csrf"], json_body=False),
            b"",
            client="cancel",
        )
        self.assertFalse(json.loads(cancelled.body)["released"])
        committed = self.app.handle(
            "POST",
            f"/api/operator/runs/{run['id']}/resolve",
            {"authorization": "Bearer operator-token-for-tests", "content-type": "application/json"},
            json.dumps({"action": "commit"}).encode(),
        )
        body = json.loads(committed.body)
        self.assertEqual(body["run"]["status"], "accepted")
        self.assertFalse(body["run"]["has_report"])
        self.assertEqual(self.ledger(cookie, app=self.app)["free_committed"], 1)

    def _arm(self, app):
        contract = self.tmp / "budget-contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "actd.engine-binding/1",
                    "start": {"method": "POST", "path": "/v1/investigations"},
                    "request_fields": ["question", "run_id"],
                    "accept_field": "id",
                    "budget": {"enforced_by": "engine"},
                }
            ),
            encoding="utf-8",
        )
        app.config.contract_path = contract
        app.config.engine_base_url = "http://127.0.0.1:9"
        app.config.engine_start_path = "/v1/investigations"
        app.config.engine_invocation_enabled = True
        app.config.max_run_budget_usd = 1


class OriginTests(Case):
    def _location(self, response):
        for key, value in response.headers:
            if key.lower() == "location":
                return value
        self.fail("missing Location")

    def test_alias_hosts_redirect_and_apex_serves_the_page(self):
        for host in ("app.aicrashtestdummies.com", "www.aicrashtestdummies.com", "APP.aicrashtestdummies.com:443"):
            response = self.app.handle("GET", "/pricing?ref=1", {"host": host}, b"")
            self.assertEqual(response.status, 301, host)
            self.assertEqual(self._location(response), "https://aicrashtestdummies.com/pricing?ref=1")
            self.assertNotIn(b"Five tests", response.body)
        posted = self.app.handle("POST", "/account/login", {"host": "www.aicrashtestdummies.com"}, b"email=a")
        self.assertEqual(posted.status, 301)
        self.assertEqual(self._location(posted), "https://aicrashtestdummies.com/account/login")
        apex = self.app.handle("GET", "/pricing", {"host": "aicrashtestdummies.com"}, b"")
        self.assertEqual(apex.status, 200)
        self.assertIn(b"Five tests are included", apex.body)
        local = self.app.handle("GET", "/docs", {}, b"")
        self.assertEqual(local.status, 200)
        self.assertIn(b"Create an account and sign in", local.body)
        quiet = self.make_app(redirect_hosts=frozenset())
        stayed = quiet.handle("GET", "/example", {"host": "app.aicrashtestdummies.com"}, b"")
        self.assertEqual(stayed.status, 200)

    def test_canonical_origin_is_configurable_and_pages_are_extensionless(self):
        app = self.make_app(canonical_origin="https://example.test", redirect_hosts=frozenset({"www.example.test"}))
        response = app.handle("HEAD", "/example.html", {"host": "www.example.test"}, b"")
        self.assertEqual(response.status, 301)
        self.assertEqual(self._location(response), "https://example.test/example.html")
        stayed = app.handle("GET", "/example", {"host": "example.test"}, b"")
        self.assertEqual(stayed.status, 200)
        self.assertIn(b"EXAMPLE", stayed.body)
        home = self.app.handle("GET", "/", {"host": "aicrashtestdummies.com"}, b"").body.decode()
        self.assertIn("What this is", home)
        self.assertIn('href="/start"', home)
        self.assertEqual(home.count("btn-primary"), 1)
        self.assertNotIn("app.aicrashtestdummies.com", home)
        css = self.app.handle("GET", "/css/site.css", {}, b"")
        self.assertEqual(css.status, 200)
        self.assertIn(b"lineup-sheet", css.body)
        icon = self.app.handle("GET", "/favicon.ico", {}, b"")
        self.assertEqual(icon.status, 200)
        self.assertTrue(icon.body.startswith(b"\x00\x00\x01\x00"))
        head = self.app.handle("HEAD", "/", {}, b"")
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        logged_out = self.app.handle("GET", "/start", {}, b"")
        self.assertEqual(logged_out.status, 303)
        self.assertEqual(self._location(logged_out), "/account/signup?next=/start")

    def test_session_cookie_and_stripe_return_use_the_canonical_origin(self):
        page = self.app.handle("GET", "/account/signup", {"host": "aicrashtestdummies.com"}, b"")
        cookie = [value for key, value in page.headers if key.lower() == "set-cookie"][0]
        self.assertNotIn("Domain=", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("SameSite=Lax", cookie)
        form = payments.checkout_form("user-1", "price_123", self.app.config.canonical_origin).decode()
        self.assertIn("aicrashtestdummies.com%2Fapp%2Fbilling%3Fstripe%3Dreturn", form)
        self.assertNotIn("app.aicrashtestdummies.com", form)

    def test_pages_export_has_no_python(self):
        import subprocess
        import sys

        subprocess.check_call([sys.executable, "scripts/export_pages.py"], cwd=ROOT)
        dist = ROOT / "dist"
        try:
            self.assertTrue((dist / "index.html").is_file())
            self.assertTrue((dist / "404.html").is_file())
            self.assertTrue((dist / "_redirects").is_file())
            self.assertEqual(list(dist.rglob("*.py")), [])
            html = (dist / "index.html").read_text()
            self.assertIn("/start", html)
            self.assertNotIn("app.aicrashtestdummies.com", html)
        finally:
            import shutil
            shutil.rmtree(dist, ignore_errors=True)


class IntakeTests(Case):
    def _form(self, cookie, fields, files=None):
        me = self.login_api(cookie)
        boundary = "actdBoundary"
        chunks = []
        fields = {"csrf": me["csrf"], "idempotency_key": "intake-key-01", **fields}
        for key, value in fields.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        for filename, data in files or []:
            chunks.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"{filename}\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode()
                + data
                + b"\r\n"
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        return self.app.handle(
            "POST",
            "/app/runs",
            {"cookie": cookie, "content-type": f"multipart/form-data; boundary={boundary}"},
            b"".join(chunks),
        )

    def test_privacy_checkbox_is_required_and_blocks_the_run(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        before = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        page = self.app.handle(
            "POST",
            "/app/runs",
            {"cookie": cookie, "content-type": "application/x-www-form-urlencoded"},
            urlencode({"csrf": me["csrf"], "idempotency_key": "privacy-key-01", "question": "Is this stored?"}).encode(),
        )
        self.assertEqual(page.status, 400)
        self.assertIn("privacy", page.body.decode().lower())
        api = self.app.handle(
            "POST",
            "/api/runs",
            self.auth_headers(cookie, me["csrf"]),
            json.dumps({"question": "Is this stored?", "idempotency_key": "privacy-key-02"}).encode(),
        )
        self.assertEqual(api.status, 400)
        self.assertEqual(json.loads(api.body)["error"], "privacy_required")
        after = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        self.assertEqual(before, after)

    def test_upload_rejects_bad_types_traversal_and_zip_bombs(self):
        import io
        import zipfile

        cookie = self.signup()
        denied = self._form(cookie, {"privacy": "yes", "question": "look"}, [("notes.exe", b"MZ")])
        self.assertEqual(denied.status, 400, denied.body[:300])
        self.assertIn("not accepted", denied.body.decode().lower())
        slipped = io.BytesIO()
        with zipfile.ZipFile(slipped, "w") as archive:
            archive.writestr("../secret.py", "print('no')\n")
        blocked = self._form(cookie, {"privacy": "yes"}, [("app.zip", slipped.getvalue())])
        self.assertEqual(blocked.status, 400, blocked.body[:400])
        self.assertIn("not allowed", blocked.body.decode().lower())
        self.app.config.max_uncompressed_bytes = 20
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w") as archive:
            archive.writestr("note.txt", "x" * 200)
        exploded = self._form(cookie, {"privacy": "yes", "idempotency_key": "intake-key-02"}, [("notes.zip", bomb.getvalue())])
        self.assertEqual(exploded.status, 400, exploded.body[:400])
        count = connect(self.app.config.db_path).execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        self.assertEqual(count, 0)
        self.assertFalse(list(self.tmp.rglob("*.zip")))
        self.assertFalse(list(self.tmp.rglob("*.exe")))

    def test_text_upload_is_sent_as_text_and_not_saved(self):
        import io
        import zipfile

        seen = {}

        def transport(run_id, question):
            seen["question"] = question
            from app.engine import EngineOutcome
            return EngineOutcome(
                accepted=True,
                disposition="accepted",
                code="finished_unconfirmed",
                detail="A provider call finished. No finding was confirmed.",
                engine_ref="boundary-run-1",
                result_json=json.dumps({"status": "completed", "model_invoked": True, "findings_confirmed": [], "crash_result": "not_confirmed"}),
            )

        self.app.config.engine_invocation_enabled = True
        self.app.config.engine_base_url = "http://127.0.0.1:5001"
        self.app.config.max_run_budget_usd = 1
        self.app.config.boundary_token = "boundary-test-token"
        self.app.config.engine_transport = transport
        cookie = self.signup()
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("src/app.py", "print('hello from the zip')\n")
            archive.writestr("assets/logo.png", b"\x89PNG\r\n")
        response = self._form(
            cookie,
            {"privacy": "yes", "intent": "find", "question": "Check this app."},
            [("project.zip", packed.getvalue())],
        )
        self.assertIn(response.status, {200, 303}, response.body[:500])
        self.assertIn("print('hello from the zip')", seen["question"])
        self.assertIn("Intent: Find anything wrong", seen["question"])
        self.assertIn("Check this app.", seen["question"])
        self.assertNotIn("logo.png", seen["question"])
        self.assertNotIn("PK", seen["question"])
        row = connect(self.app.config.db_path).execute("SELECT question FROM runs").fetchone()
        self.assertEqual(row["question"], seen["question"])
        self.assertFalse(list(self.tmp.rglob("*.zip")))
        run_id = connect(self.app.config.db_path).execute("SELECT id FROM runs").fetchone()["id"]
        page = self.app.handle("GET", f"/app/runs/{run_id}", {"cookie": cookie}, b"").body.decode()
        self.assertIn("Report", page)
        self.assertIn("print(&#x27;hello from the zip&#x27;)", page.replace("&#39;", "&#x27;"))
        self.assertIn("overflow-wrap", self.app.handle("GET", "/css/site.css", {}, b"").body.decode())


def _team_talk_server(hits, seats_called=False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/version":
                body = b'{"version":"78"}'
            elif self.path == "/api/lab/crash/modes":
                body = json.dumps(
                    {
                        "protocol": "0.1.0",
                        "intensity": {"QUICK": {"roster": "recorded, seats are not called by intake"}},
                    }
                ).encode()
            elif self.path.startswith("/api/lab/crash/") and self.path.endswith("/report.json"):
                body = b'{"detail":"no such campaign"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                hits.append(("GET", self.path, b""))
                return
            elif self.path.startswith("/api/lab/crash/"):
                campaign = self.path.split("/")[-1]
                body = json.dumps(
                    {
                        "campaign_id": campaign,
                        "seats_called": seats_called,
                        "seats_attached": ["recorded-seat"],
                        "idle": not seats_called,
                        "laboratory": {"status": "BOUND", "authorized": False, "executable_now": False},
                        "events": [],
                        "card": {"campaign_id": campaign, "tests_executed": 0},
                    }
                ).encode()
            else:
                body = b'{"detail":"not found"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            hits.append(("GET", self.path, b""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            hits.append(("POST", self.path, raw))
            if self.path == "/api/lab/crash/open":
                payload = json.loads(raw.decode())
                if payload.get("track") not in {"CODE", "AI", "DOCUMENT", "OTHER"} or not payload.get("session_id"):
                    body = b'{"status":"REJECTED","summary":"track"}'
                    code = 200
                else:
                    body = b'{"status":"BOUND","campaign_id":"camp-synthetic-1"}'
                    code = 200
            else:
                body = b'{"detail":"billable route"}'
                code = 403
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _engine_server(hits, status, body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in {"/api/version", "/api/lab/crash/modes"}:
                payload = (
                    b'{"version":"78"}'
                    if self.path == "/api/version"
                    else b'{"protocol":"0.1.0","intensity":{"QUICK":{"roster":"recorded, seats are not called by intake"}}}'
                )
                code = 200
            else:
                payload = b'{"detail":"no such campaign"}'
                code = 404
            hits.append((self.path, b""))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            hits.append((self.path, raw))
            data = body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _http(port, method, path, body, headers):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body if method == "POST" else None,
        method=method,
        headers=headers,
    )
    try:
        with opener.open(request) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


if __name__ == "__main__":
    unittest.main()
