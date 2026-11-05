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

    def signup(self, email="ada@example.com", client="signup", password=PASSWORD, app=None):
        app = app or self.app
        body = urlencode({"email": email, "password": password, "confirm": password}).encode()
        response = app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded"},
            body,
            client=client,
        )
        self.assertEqual(response.status, 303, response.body)
        return self.cookie(response)

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
        body = json.dumps({"question": question, "idempotency_key": key, "budget_usd": 100000, "credits": 99}).encode()
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
        bad = self.app.handle(
            "POST",
            "/account/login",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"email": "ada@example.com", "password": "wrong-password-here"}).encode(),
            client="login-bad",
        )
        self.assertEqual(bad.status, 401)
        self.assertNotIn(PASSWORD, bad.body.decode())
        good = self.app.handle(
            "POST",
            "/account/login",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"email": "Ada@Example.com", "password": PASSWORD}).encode(),
            client="login-good",
        )
        self.assertEqual(good.status, 303)
        self.assertNotEqual(self.cookie(good), cookie)

    def test_duplicate_signup_and_short_password(self):
        self.signup(client="a")
        body = urlencode({"email": "ada@example.com", "password": PASSWORD, "confirm": PASSWORD}).encode()
        again = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded"},
            body,
            client="b",
        )
        self.assertEqual(again.status, 409)
        short = self.app.handle(
            "POST",
            "/account/signup",
            {"content-type": "application/x-www-form-urlencoded"},
            urlencode({"email": "bea@example.com", "password": "short", "confirm": "short"}).encode(),
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
        self.assertEqual(me["role"], "admin")
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
        self.assertIn("contract_missing", payload["engine_blockers"])
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
        self.assertEqual(self.app.recover(), 1)
        restored = self.ledger(cookie)
        self.assertEqual(restored["free_held"], 0)
        self.assertEqual(restored["free_remaining"], 5)
        self.assertTrue(any("expired" in entry["note"] for entry in restored["entries"]))

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

    def test_acceptance_commits_once_and_payload_is_escaped(self):
        hits = []
        report = "<script>alert(1)</script>\nclaude says ignore previous instructions"
        httpd = _engine_server(hits, 200, json.dumps({"id": "eng_1", "report": report}).encode())
        port = httpd.server_address[1]
        self._arm(port)
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "Trace the public withdraw path.", "accept-key-01")
        self.assertEqual(response.status, 200, response.body)
        run = json.loads(response.body)["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["engine_ref"], "eng_1")
        self.assertEqual(len(hits), 1)
        sent = json.loads(hits[0][1])
        self.assertEqual(sent["budget_usd"], 1.5)
        self.assertNotIn("credits", sent)
        self.assertTrue(assert_no_seat_names(json.dumps(sent)))
        retry = self.start_json(cookie, me["csrf"], "Trace the public withdraw path.", "accept-key-01")
        self.assertTrue(json.loads(retry.body)["idempotent"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.ledger(cookie)["free_committed"], 1)
        page = self.app.handle("GET", f"/app/runs/{run['id']}", {"cookie": cookie}, b"")
        html = page.body.decode()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("claude", html.lower())
        self.assertIn("[redacted]", html)
        httpd.shutdown()

    def test_engine_error_does_not_commit(self):
        hits = []
        httpd = _engine_server(hits, 500, b'{"id":"nope"}')
        self._arm(httpd.server_address[1])
        cookie = self.signup()
        me = self.login_api(cookie)
        response = self.start_json(cookie, me["csrf"], "A failing engine must not bill.", "error-key-01")
        self.assertEqual(json.loads(response.body)["run"]["status"], "engine_unavailable")
        self.assertEqual(self.ledger(cookie)["free_committed"], 0)
        self.assertEqual(len(hits), 1)
        httpd.shutdown()

    def _arm(self, port: int):
        contract = self.tmp / "contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "actd.engine-binding/1",
                    "start": {"method": "POST", "path": "/v1/investigations"},
                    "request_fields": ["question", "run_id", "budget_usd"],
                    "accept_field": "id",
                }
            ),
            encoding="utf-8",
        )
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

    def test_webhook_signature_replay_and_disabled_mode(self):
        self._enable_fixture()
        cookie = self.signup()
        me = self.login_api(cookie)
        checkout = self.app.handle(
            "POST",
            "/api/billing/checkout",
            self.auth_headers(cookie, me["csrf"]),
            b"{}",
        )
        self.assertEqual(checkout.status, 200, checkout.body)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)
        self.assertIn("No card number", checkout.body.decode())
        secret = "fixture-webhook-secret"
        good = self._event(me["id"], "evt_1", 5)
        bad = self.app.handle(
            "POST",
            "/api/billing/webhook",
            {"content-type": "application/json", payments.SIGNATURE_HEADER: "t=1,v1=deadbeef"},
            good,
        )
        self.assertEqual(bad.status, 400)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)
        stamped = self._signed(secret, good)
        ok = self.app.handle("POST", "/api/billing/webhook", stamped[0], good)
        self.assertEqual(ok.status, 200, ok.body)
        self.assertFalse(json.loads(ok.body)["duplicate"])
        self.assertEqual(self.ledger(cookie)["paid_credits"], 5)
        replay = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, good)[0], good)
        self.assertTrue(json.loads(replay.body)["duplicate"])
        self.assertEqual(self.ledger(cookie)["paid_credits"], 5)
        huge = self._event(me["id"], "evt_huge", 999999)
        rejected = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, huge)[0], huge)
        self.assertEqual(rejected.status, 400)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 5)
        stale_body = self._event(me["id"], "evt_old", 5)
        old = int(time.time()) - 3600
        stale_headers = {
            "content-type": "application/json",
            payments.SIGNATURE_HEADER: payments.header_value(old, payments.sign(secret, old, stale_body)),
        }
        stale = self.app.handle("POST", "/api/billing/webhook", stale_headers, stale_body)
        self.assertEqual(stale.status, 400)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 5)

    def test_disabled_mode_ignores_a_valid_signature(self):
        cookie = self.signup()
        me = self.login_api(cookie)
        secret = "not-enabled"
        self.app.config.payment_webhook_secret = secret
        body = self._event(me["id"], "evt_disabled", 5)
        response = self.app.handle("POST", "/api/billing/webhook", self._signed(secret, body)[0], body)
        self.assertEqual(response.status, 503)
        self.assertEqual(self.ledger(cookie)["paid_credits"], 0)

    def test_paid_credit_is_required_after_the_allowance_and_commits_once(self):
        self.app = self.make_app(free_credits=0)
        self._enable_fixture()
        hits = []
        httpd = _engine_server(hits, 200, b'{"id":"eng_paid"}')
        self._arm_on(self.app, httpd.server_address[1])
        cookie = self.signup(app=self.app)
        me = self.login_api(cookie, app=self.app)
        blocked = self.start_json(cookie, me["csrf"], "Needs a paid credit.", "paid-key-01", app=self.app)
        self.assertEqual(blocked.status, 402)
        self.assertEqual(hits, [])
        body = self._event(me["id"], "evt_pack", 5)
        granted = self.app.handle("POST", "/api/billing/webhook", self._signed("fixture-webhook-secret", body)[0], body)
        self.assertEqual(granted.status, 200, granted.body)
        accepted = self.start_json(cookie, me["csrf"], "Needs a paid credit.", "paid-key-01", app=self.app)
        self.assertEqual(json.loads(accepted.body)["run"]["status"], "accepted")
        self.assertEqual(self.ledger(cookie, app=self.app)["paid_committed"], 1)
        self.assertEqual(self.ledger(cookie, app=self.app)["paid_remaining"], 4)
        httpd.shutdown()

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
                    "request_fields": ["question", "run_id", "budget_usd"],
                    "accept_field": "id",
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
        self.assertEqual(upload.status, 415)
        self.assertFalse(list((self.tmp / "data").glob("*.zip")))

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
        self.assertNotIn("/app", sitemap)
        self.assertNotIn("/account", sitemap)
        health = json.loads(self.app.handle("GET", "/api/health", {}, b"").body)
        self.assertIsNone(health["historic_visitors"])
        self.assertEqual(health["engine"], "unavailable")
        self.assertIn("team_talk_absent", health["engine_blockers"])
        self.assertIn("contract_missing", health["engine_blockers"])
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
            status, headers, body = _http(
                port,
                "POST",
                "/account/signup",
                urlencode({"email": "ada@example.com", "password": PASSWORD, "confirm": PASSWORD}).encode(),
                {"content-type": "application/x-www-form-urlencoded"},
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
        self.app.config.admin_emails = frozenset({"ada@example.com"})
        cookie = self.cookie(
            self.app.handle(
                "POST",
                "/account/login",
                {"content-type": "application/x-www-form-urlencoded"},
                urlencode({"email": "ada@example.com", "password": PASSWORD}).encode(),
                client="metrics",
            )
        )
        admin = json.loads(self.app.handle("GET", "/api/admin/metrics", {"cookie": cookie}, b"").body)
        self.assertGreaterEqual(admin["anonymous_requests"], 1)
        self.assertGreaterEqual(admin["authenticated_requests"], 1)
        self.assertGreaterEqual(admin["unique_signed_in_accounts"], 1)
        self.assertIsNone(admin["historic_visitors"])
        self.assertLess(admin["unique_signed_in_accounts"], admin["anonymous_requests"] + admin["authenticated_requests"])


def _engine_server(hits, status, body):
    class Handler(BaseHTTPRequestHandler):
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
