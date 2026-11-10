"""HTTP application. Auth is checked on the server for every account action."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

from app import analytics, engine, intake, ledger, payments, visitors
from app.boundary_client import usage_from_json
from app.auth import (
    client_bucket,
    dummy_verify,
    hash_password,
    hash_token,
    iso,
    new_csrf,
    new_token,
    normalize_email,
    parse_iso,
    password_problem,
    session_expiry,
    utcnow,
    verify_password,
)
from app.config import Config
from app.db import connect
from app.pages import (
    admin_page,
    auth_page,
    billing_page,
    dashboard,
    message_page,
    not_found,
    operator_login_page,
    run_page,
    start_page,
    visitors_dashboard,
)

log = logging.getLogger("actd")
RECOVER_TEXT = (
    "Password reset is unavailable. No mail provider is configured. "
    "Submitting this page does not change a password and does not say whether an account exists."
)

ID_RE = re.compile(r"^[a-f0-9]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}
BLOCKED_PREFIXES = ("engine/", "app/", "tests/", "docs/", ".git/", "data/", "private/")
SEAT_NAMES = ("claude", "chatgpt", "grok", "gemini", "deepseek", "flint")


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


class App:
    def __init__(self, config: Config):
        self.config = config
        # Open once so the schema exists before concurrent requests.
        conn = connect(config.db_path)
        conn.close()
        self.recover()

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes, client: str = "local") -> Response:
        method = method.upper()
        parsed = urlparse(target)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        headers = {str(k).lower(): v for k, v in headers.items()}
        redirect = self._alias_redirect(headers, path, parsed.query)
        if redirect is not None:
            return redirect
        try:
            if method not in {"GET", "POST", "HEAD", "OPTIONS"}:
                return self._text(405, "Method not allowed")
            if "transfer-encoding" in headers:
                return self._text(400, "Send a Content-Length instead of a transfer encoding.")
            if len(body) > self._body_limit(method, path):
                return self._text(413, "Request body is too large.")
            response = self._route(method, path, parsed.query, headers, body, client)
        except Exception as exc:
            log.error("request failed: %s %s (%s)", method, _path_class(path), type(exc).__name__)
            response = self._html(
                500,
                message_page(
                    "Something went wrong",
                    "That request failed. A credit already handed to the engine stays held until an operator resolves it.",
                ),
            )
        if method == "HEAD":
            length = str(len(response.body))
            response.body = b""
            if not any(key.lower() == "content-length" for key, _value in response.headers):
                response.headers.append(("Content-Length", length))
        return response

    def _alias_redirect(self, headers, path, query) -> Response | None:
        """Send app and www to the same path on the canonical origin.

        The Host header is compared to an allow-list. It is never copied into
        the Location. Requests with no Host, or a Host outside that list
        (localhost, the apex itself), are served here.
        """
        hostname = _hostname(headers.get("host", ""))
        aliases = {item.lower().rstrip(".") for item in self.config.redirect_hosts}
        origin = self.config.canonical_origin.rstrip("/")
        canonical_host = _hostname(urlparse(origin).netloc)
        if not hostname or hostname == canonical_host or hostname not in aliases:
            return None
        if not path.startswith("/") or path.startswith("//"):
            path = "/"
        location = origin + path
        if query:
            location += "?" + query.replace("\r", "").replace("\n", "")
        return Response(
            301,
            [("Location", location), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")],
            b"",
        )

    def _route(self, method, path, query, headers, body, client) -> Response:
        now = utcnow()
        now_iso = iso(now)
        conn = connect(self.config.db_path)
        try:
            user = self._user(conn, headers, now)
            self._count(conn, now, user, path)
            identity = client_identity(headers, client, self.config)
            if method == "OPTIONS" and path == "/v/hit":
                return self._visitor_cors(headers)
            if method in {"GET", "HEAD"}:
                return self._get(conn, path, query, headers, user, client, now, method)
            return self._post(conn, path, query, headers, body, user, identity, now, now_iso)
        finally:
            conn.close()

    def _get(self, conn, path, query, headers, user, client, now, method) -> Response:
        if method == "GET":
            self._note_page(conn, headers, client, path, now)
        if path == "/robots.txt":
            return self._robots()
        if path == "/sitemap.xml":
            return self._sitemap()
        if path == "/favicon.ico":
            return self._static("/favicon.ico")
        if path in {"/", "/index.html"}:
            return self._static("/index.html")
        if path in {"/pricing", "/pricing.html", "/docs", "/docs.html", "/example", "/example.html"}:
            file_path = {
                "/pricing": "/pricing.html",
                "/docs": "/docs.html",
                "/example": "/example.html",
            }.get(path, path)
            return self._static(file_path)
        if path in {"/start", "/start.html"}:
            if not user:
                return self._redirect("/account/signup?next=/start")
            return self._html(200, self._start_form(conn, user))
        if path == "/v/hit":
            return self._visitor_hit(conn, headers, query, client, now)
        if path == "/admin/visitors":
            return self._visitors_page(conn, headers, now)
        if path == "/api/health":
            return self._json(200, self.health())
        if path == "/account/signup":
            if user:
                return self._redirect("/app")
            return self._form_page("signup", next_path=_safe_next(query))
        if path == "/account/login":
            if user:
                return self._redirect(_safe_next(query))
            return self._form_page("login", next_path=_safe_next(query))
        if path == "/account/logout":
            return self._redirect("/")
        if path == "/account/recover":
            return self._html(200, message_page("Password reset is off", RECOVER_TEXT, user=user))
        if path == "/app":
            if not user:
                return self._redirect("/account/login?next=/app")
            return self._html(200, self._dashboard(conn, user))
        if path == "/app/billing":
            if not user:
                return self._redirect("/account/login?next=/app/billing")
            view = ledger.snapshot(conn, user["id"], lock_unverified=self.config.require_verified_email)
            return self._html(200, billing_page(user, view, payments.public_status(self.config)))
        if path == "/admin":
            if not user or user["role"] != "admin":
                return self._html(404, not_found())
            return self._html(200, admin_page(user, analytics.metrics(conn), self._gates()))
        if path == "/api/me":
            if not user:
                return self._json(401, {"error": "auth_required"})
            return self._json(200, {"id": user["id"], "email": user["email"], "role": user["role"], "csrf": user["csrf"]})
        if path == "/api/ledger":
            if not user:
                return self._json(401, {"error": "auth_required"})
            return self._json(200, ledger.snapshot(conn, user["id"], lock_unverified=self.config.require_verified_email))
        if path == "/api/runs":
            if not user:
                return self._json(401, {"error": "auth_required"})
            return self._json(200, {"runs": _public_runs(ledger.list_runs(conn, user["id"]))})
        if path.startswith("/app/runs/"):
            if not user:
                return self._redirect("/account/login?next=" + quote(path, safe="/"))
            run = self._owned_run(conn, user, path.rsplit("/", 1)[-1])
            if not run:
                return self._html(404, not_found())
            return self._html(200, run_page(user, run))
        if path.startswith("/api/runs/"):
            if not user:
                return self._json(401, {"error": "auth_required"})
            run = self._owned_run(conn, user, path.rsplit("/", 1)[-1])
            if not run:
                return self._json(404, {"error": "not_found"})
            return self._json(200, _public_run(run))
        if path == "/api/admin/metrics":
            if not user or user["role"] != "admin":
                return self._json(404, {"error": "not_found"})
            return self._json(200, analytics.metrics(conn))
        if path == "/api/billing":
            if not user:
                return self._json(401, {"error": "auth_required"})
            return self._json(200, payments.public_status(self.config))
        static = self._static(path)
        if static.status != 404:
            return static
        return self._html(404, not_found())

    def _post(self, conn, path, query, headers, body, user, client, now, now_iso) -> Response:
        upload = None
        content_type = headers.get("content-type", "")
        if "multipart/" in content_type:
            if path not in {"/app/runs", "/api/runs"}:
                return self._html(
                    415,
                    message_page(
                        "Uploads are not accepted",
                        "A file can be attached only on the crash-test form. It is not saved.",
                        user=user,
                    ),
                )
            try:
                form, upload = intake.parse_multipart(content_type, body, self.config.max_upload_bytes)
            except intake.IntakeError as exc:
                if not user:
                    return self._redirect("/account/login?next=/start")
                if path.startswith("/api/"):
                    return self._json(400, {"error": exc.code})
                return self._html(400, self._start_form(conn, user, error=[exc.message]))
        else:
            form = _parse_body(headers, body)
        if path == "/v/hit":
            return self._visitor_hit(conn, headers, query, client, now, body)
        if path == "/admin/operator-login":
            return self._operator_login(conn, form, now)
        if path == "/admin/operator-logout":
            return self._operator_logout(headers)
        if path == "/account/recover":
            return self._html(200, message_page("Password reset is off", RECOVER_TEXT, user=user))
        if path == "/account/signup":
            if not self._rate(conn, "signup", client, now, kind="signup"):
                return self._form_page("signup", status=429, error=["Too many account attempts from this network. Wait and try again."])
            if not self._form_ok(headers, form):
                return self._form_page("signup", status=403, error=["Reload the page and submit it again."])
            return self._signup(conn, form, client, now)
        if path == "/account/login":
            if not self._rate(conn, "auth", client, now):
                return self._form_page("login", status=429, error=["Too many sign-in attempts from this network. Wait and try again."])
            if not self._form_ok(headers, form):
                return self._form_page("login", status=403, error=["Reload the page and submit it again."])
            return self._login(conn, form, client, now)
        if path == "/api/sessions/revoke":
            if not user:
                return self._json(401, {"error": "auth_required"})
            if not _csrf_ok(user, headers, form):
                return self._json(403, {"error": "bad_csrf"})
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            response = self._json(200, {"ok": True})
            response.headers.append(("Set-Cookie", f"{self.config.cookie_name}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"))
            return response
        if path == "/account/logout":
            if not user:
                return self._redirect("/account/login")
            if not _csrf_ok(user, headers, form):
                return self._html(403, message_page("Form rejected", "Reload the page and submit it again.", user=user))
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (user["token_hash"],))
            return self._redirect("/", clear_cookie=True)
        if path in {"/app/runs", "/api/runs"}:
            if not user:
                if path.startswith("/api/"):
                    return self._json(401, {"error": "auth_required"})
                return self._redirect("/account/login?next=/start")
            if not _csrf_ok(user, headers, form):
                return self._reject_csrf(path, user)
            return self._start(conn, user, form, now, now_iso, as_json=path.startswith("/api/"), upload=upload)
        if path == "/api/billing/checkout" or path == "/app/billing/checkout":
            if not user:
                return self._json(401, {"error": "auth_required"})
            if not _csrf_ok(user, headers, form):
                return self._json(403, {"error": "bad_csrf"})
            return self._checkout(conn, user, now_iso, as_json=path.startswith("/api/"))
        if path == "/api/billing/stripe-webhook":
            return self._stripe_webhook(conn, headers, body, now_iso)
        if path == "/api/billing/webhook":
            return self._webhook(conn, headers, body, now, now_iso)
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            if not user:
                return self._json(401, {"error": "auth_required"})
            if not _csrf_ok(user, headers, form):
                return self._json(403, {"error": "bad_csrf"})
            run_id = path[len("/api/runs/") : -len("/cancel")]
            return self._cancel(conn, user, run_id, now_iso)
        if path == "/api/operator/promote" or path == "/api/operator/verify" or (
            path.startswith("/api/operator/runs/") and path.endswith("/resolve")
        ):
            if not self._operator_ok(headers):
                return self._json(404, {"error": "not_found"})
            return self._operator(conn, path, form, now_iso)
        return self._html(404, not_found())

    def health(self) -> dict:
        reasons = engine.blockers(self.config)
        payment = payments.public_status(self.config)
        return {
            "ok": True,
            "auth": "ready",
            "free_credits": self.config.free_credits,
            "engine": "armed" if not [item for item in reasons if item != "team_talk_absent"] else "unavailable",
            "engine_blockers": reasons,
            "payments": payment["mode"] if payment["enabled"] else "disabled",
            "model_cost_policy": "on_file" if self.config.model_cost_recorded() else "missing",
            "customer_price": payment["price"],
            "collects_card_numbers": False,
            "historic_visitors": None,
            "email_verification": "missing",
            "admin_from_unverified_email": False,
            "public_launch": False,
            "operator_resolution": "configured" if self.config.operator_token else "missing",
            "team_talk_contract": "aict.boundary.v1",
            "billable_engine_routes": "blocked",
            "boundary_token": "configured" if self.config.boundary_token else "missing",
            "public_tunnel_unauthenticated": True,
        }

    def _gates(self) -> dict:
        payment = payments.public_status(self.config)
        reasons = engine.blockers(self.config)
        armed = not [item for item in reasons if item != "team_talk_absent"]
        return {
            "payments": payment["mode"] if payment["enabled"] else "disabled",
            "customer_price": "not set" if not payment.get("price") else "configured",
            "model_cost_policy": "on file" if self.config.model_cost_recorded() else "missing",
            "engine": "armed" if armed else "unavailable",
        }

    def recover(self) -> dict:
        conn = connect(self.config.db_path)
        try:
            from app.db import tx

            def work():
                return ledger.sweep(conn, iso(utcnow()), self.config.reservation_ttl_seconds)

            released = tx(conn, work)
            unknown = ledger.quarantine_dispatching(conn, iso(utcnow()), 0)
            return {"released_unsent": released, "unknown": unknown}
        finally:
            conn.close()

    def _note_page(self, conn, headers, client, path, now) -> None:
        counted = {
            "/", "/index.html", "/start", "/start.html", "/pricing", "/pricing.html",
            "/docs", "/docs.html", "/example", "/example.html", "/app", "/app/billing",
            "/account/login", "/account/signup",
        }
        if path not in counted:
            return
        self._store_visit(conn, headers, client, path, now, source="app_html", human=False)

    def _visitor_hit(self, conn, headers, query, client, now, body: bytes = b"") -> Response:
        from urllib.parse import parse_qs
        fields = {key: values[-1] for key, values in parse_qs(query).items()}
        if body and not fields.get("path"):
            extra = _parse_body(headers, body)
            fields.update({key: str(value) for key, value in extra.items()})
        path = visitors.clean_path(fields.get("path") or "/")
        referrer = visitors.clean_referrer(fields.get("ref") or headers.get("referer") or "")
        human = fields.get("human") == "1"
        kind = visitors.classify_user_agent(headers.get("user-agent", ""))
        ip = visitors.client_ip(headers, client)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        hashed = visitors.ip_hash(self.config.secret, day, ip)
        allowed = analytics.allow(conn, "beacon:" + hashed, int(now.timestamp()), 3600, 60)
        if not allowed:
            return Response(204, self._visitor_cors_headers(headers), b"")
        if kind == "bot":
            visitors.record(
                conn, day=day, ts=iso(now), ip_hash_value=hashed, kind="bot",
                path=path, referrer=referrer, source="beacon",
            )
        elif human:
            visitors.mark_human(conn, day=day, ip_hash_value=hashed, path=path, ts=iso(now))
        elif not visitors.recent_hit(conn, day=day, ip_hash_value=hashed, path=path, ts=iso(now)):
            visitors.record(
                conn, day=day, ts=iso(now), ip_hash_value=hashed, kind=kind,
                path=path, referrer=referrer, source="beacon",
            )
        return Response(204, self._visitor_cors_headers(headers), b"")

    def _store_visit(self, conn, headers, client, path, now, *, source: str, human: bool) -> None:
        kind = visitors.classify_user_agent(headers.get("user-agent", ""))
        if human and kind != "bot":
            kind = "human"
        ip = visitors.client_ip(headers, client)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        hashed = visitors.ip_hash(self.config.secret, day, ip)
        if not analytics.allow(conn, "pageview:" + hashed, int(now.timestamp()), 3600, 120):
            return
        visitors.record(
            conn, day=day, ts=iso(now), ip_hash_value=hashed, kind=kind,
            path=visitors.clean_path(path), referrer=visitors.clean_referrer(headers.get("referer") or ""),
            source=source,
        )

    def _visitor_cors(self, headers) -> Response:
        return Response(204, self._visitor_cors_headers(headers), b"")

    def _visitor_cors_headers(self, headers) -> list[tuple[str, str]]:
        origin = headers.get("origin", "")
        allowed = {"https://aicrashtestdummies.com", "https://www.aicrashtestdummies.com", "https://app.aicrashtestdummies.com"}
        headers_out = [("Cache-Control", "no-store"), ("Access-Control-Allow-Methods", "GET, POST, OPTIONS")]
        if origin in allowed:
            headers_out.append(("Access-Control-Allow-Origin", origin))
            headers_out.append(("Vary", "Origin"))
        return self._security(headers_out)

    def _operator_session(self, conn, headers, now):
        token = _cookie(headers.get("cookie", ""), "actd_operator")
        if not token:
            return False
        row = conn.execute(
            "SELECT expires_at FROM operator_sessions WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return False
        if parse_iso(row["expires_at"]) <= now:
            conn.execute("DELETE FROM operator_sessions WHERE token_hash = ?", (hash_token(token),))
            return False
        return True

    def _operator_login(self, conn, form, now) -> Response:
        expected = self.config.operator_token or ""
        if not expected:
            return self._html(404, not_found())
        presented = str(form.get("token", ""))
        if len(presented) != len(expected) or not hmac.compare_digest(presented, expected):
            return self._html(401, operator_login_page("That token was not accepted."))
        token = new_token()
        conn.execute(
            "INSERT INTO operator_sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), iso(now), iso(session_expiry(now, 12 * 3600))),
        )
        response = self._redirect("/admin/visitors")
        response.headers.append(("Set-Cookie", self._named_cookie("actd_operator", token, 12 * 3600)))
        return response

    def _operator_logout(self, headers) -> Response:
        response = self._redirect("/")
        response.headers.append(("Set-Cookie", "actd_operator=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"))
        return response

    def _visitors_page(self, conn, headers, now) -> Response:
        if not self.config.operator_token:
            return self._html(404, not_found())
        if not self._operator_session(conn, headers, now):
            return self._html(401, operator_login_page(""))
        return self._html(200, visitors_dashboard(visitors.summary(conn)))

    def _robots(self) -> Response:
        origin = self.config.canonical_origin.rstrip("/")
        body = (
            "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nDisallow: /engine\nDisallow: /v/\n\n"
            f"Sitemap: {origin}/sitemap.xml\n"
        )
        return Response(200, [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "public, max-age=3600")], body.encode())

    def _sitemap(self) -> Response:
        origin = self.config.canonical_origin.rstrip("/")
        paths = [
            "/",
            "/start",
            "/pricing",
            "/pricing.html",
            "/docs",
            "/docs.html",
            "/example",
            "/example.html",
            "/account/signup",
            "/account/login",
        ]
        urls = "\n".join(f"  <url><loc>{origin}{path}</loc></url>" for path in paths)
        body = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n'
        return Response(200, [("Content-Type", "application/xml; charset=utf-8"), ("Cache-Control", "public, max-age=3600")], body.encode())

    def _signup(self, conn, form, client, now) -> Response:
        email = normalize_email(form.get("email", ""))
        password = form.get("password", "")
        confirm = form.get("confirm", "")
        problems = []
        if not email:
            problems.append("Enter a valid email address.")
        problem = password_problem(password)
        if problem:
            problems.append(problem)
        if password != confirm:
            problems.append("The two passwords do not match.")
        if problems:
            return self._form_page("signup", status=400, error=problems, email=email or "")
        from app.db import tx

        user_id = ledger.new_id()
        role = "user"

        def work():
            exists = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at, role, email_verified) VALUES (?, ?, ?, ?, ?, 0)",
                (user_id, email, hash_password(password), iso(now), role),
            )
            ledger.ensure_account(conn, user_id, self.config.free_credits)
            return True

        try:
            created = tx(conn, work)
        except Exception as exc:
            log.error("signup failed (%s)", type(exc).__name__)
            return self._form_page("signup", status=500, error=["The account was not created."], email=email or "")
        if not created:
            return self._form_page("signup", status=409, error=["An account with that email already exists."], email=email or "")
        return self._open_session(conn, user_id, email, role, now, _safe_next_value(str(form.get("next") or "/start")))

    def _login(self, conn, form, client, now) -> Response:
        email = normalize_email(form.get("email", ""))
        password = form.get("password", "")
        next_path = _safe_next_value(form.get("next", "/app"))
        if email and not self._rate(conn, "login-email", email, now):
            return self._form_page(
                "login",
                status=429,
                error=["Too many sign-in attempts for this email. Wait and try again."],
                email=email,
                next_path=next_path,
            )
        row = None
        if email:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or not verify_password(password, row["password_hash"] if row else ""):
            if row is None:
                dummy_verify(password if isinstance(password, str) else "")
            return self._form_page(
                "login",
                status=401,
                error=["Email or password is wrong."],
                email=email or "",
                next_path=next_path,
            )
        return self._open_session(conn, row["id"], row["email"], row["role"], now, next_path)

    def _open_session(self, conn, user_id, email, role, now, location) -> Response:
        token = new_token()
        csrf = new_csrf()
        expires = session_expiry(now, self.config.session_seconds)
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hash_token(token), user_id, csrf, iso(now), iso(expires)),
        )
        response = self._redirect(location)
        response.headers.append(("Set-Cookie", self._cookie(token, int(self.config.session_seconds))))
        return response

    def _body_limit(self, method: str, path: str) -> int:
        if method == "POST" and path in {"/app/runs", "/api/runs"}:
            return self.config.max_upload_bytes + 80_000
        return self.config.max_body_bytes

    def _start_form(self, conn, user, error=None, question="", paste="") -> str:
        return start_page(
            user,
            idempotency_key=new_token()[:24],
            error=error,
            question=question if isinstance(question, str) else "",
            paste=paste if isinstance(paste, str) else "",
            cap=self.config.input_char_cap,
        )

    def _start(self, conn, user, form, now, now_iso, *, as_json: bool, upload=None) -> Response:
        if not self._rate(conn, "start:" + user["id"], user["id"], now, kind="start"):
            message = "Too many test attempts on this account for the current hour."
            if as_json:
                return self._json(429, {"error": "rate_limited"})
            return self._html(429, self._start_form(conn, user, error=[message], question=str(form.get("question") or "")[:4000]))
        try:
            question = intake.assemble(
                form,
                upload,
                self.config.input_char_cap,
                {
                    "max_upload": self.config.max_upload_bytes,
                    "max_uncompressed": self.config.max_uncompressed_bytes,
                },
            )
        except intake.IntakeError as exc:
            if as_json:
                return self._json(400, {"error": exc.code})
            return self._html(
                400,
                self._start_form(
                    conn,
                    user,
                    error=[exc.message],
                    question=str(form.get("question") or "")[:4000],
                    paste=str(form.get("paste") or "")[:4000],
                ),
            )
        key = str(form.get("idempotency_key", "")).strip()
        form.pop("owner_id", None)
        form.pop("local_path", None)
        form.pop("git", None)
        form.pop("zip_b64", None)
        form.pop("budget_usd", None)
        form.pop("credits", None)
        if not KEY_RE.match(key):
            if as_json:
                return self._json(400, {"error": "bad_idempotency_key"})
            key = new_token()[:24]
            return self._html(400, self._start_form(conn, user, error=["The form expired. Submit it once more."], question=question))
        result = ledger.reserve(
            conn,
            user["id"],
            key,
            question,
            now_iso,
            self.config.reservation_ttl_seconds,
            lock_unverified=self.config.require_verified_email,
        )
        if not result["ok"]:
            if result.get("error") == "verification_required":
                if as_json:
                    return self._json(403, {"error": "verification_required"})
                return self._html(403, self._start_form(conn, user, error=["Confirm email ownership before starting a test. No mail provider is configured."]))
            if as_json:
                return self._json(402, {"error": "payment_required", "ledger": ledger.snapshot(conn, user["id"], lock_unverified=self.config.require_verified_email)})
            return self._redirect("/app/billing")
        if not result["created"]:
            run = result["run"]
            if run.get("status") != "retry_later":
                if as_json:
                    return self._json(200, {"idempotent": True, "run": _public_run(run)})
                return self._redirect(f"/app/runs/{run['id']}")
            result = {
                "created": True,
                "reservation_id": run["reservation_id"],
                "run_id": run["id"],
            }
        if self.config.before_engine:
            self.config.before_engine({"run_id": result["run_id"], "reservation_id": result["reservation_id"]})
        current = ledger.read_reservation(conn, result["reservation_id"])
        if current is None or current["state"] != "reserved":
            run = ledger.read_run(conn, user["id"], result["run_id"])
            if as_json:
                return self._json(200, {"run": _public_run(run)})
            return self._redirect(f"/app/runs/{result['run_id']}")
        will_call = not [item for item in engine.blockers(self.config) if item != "team_talk_absent"]
        if will_call:
            ledger.mark_dispatching(conn, result["reservation_id"], iso(utcnow()))
            if self.config.after_dispatch:
                self.config.after_dispatch({"run_id": result["run_id"], "reservation_id": result["reservation_id"]})
            current = ledger.read_reservation(conn, result["reservation_id"])
            if current is None or current["state"] != "dispatching":
                run = ledger.read_run(conn, user["id"], result["run_id"])
                if as_json:
                    return self._json(200, {"run": _public_run(run)})
                return self._redirect(f"/app/runs/{result['run_id']}")
        outcome = engine.invoke(
            self.config,
            run_id=result["run_id"],
            question=question,
            owner_id=user["id"],
            idempotency_key=key,
        )
        now_done = iso(utcnow())
        if outcome.disposition == "retry":
            ledger.reopen_for_retry(conn, result["reservation_id"], now_done, outcome.detail)
            if as_json:
                return self._json(429, {"error": "concurrency_limit", "idempotency_key": key})
            return self._html(429, self._start_form(conn, user, error=[outcome.detail], question=question))
        if outcome.disposition == "ambiguous":
            recorded = ledger.mark_unknown(conn, result["reservation_id"], now_done, outcome.detail)
            run = recorded.get("run") or ledger.read_run(conn, user["id"], result["run_id"])
            if run and outcome.result_json:
                conn.execute("UPDATE runs SET result_json = ? WHERE id = ? AND user_id = ?", (outcome.result_json, run["id"], user["id"]))
                run = ledger.read_run(conn, user["id"], result["run_id"])
            if as_json:
                return self._json(202, {"run": _public_run(run), "engine_blockers": outcome.blockers or []})
            return self._redirect(f"/app/runs/{run['id']}")
        status = _run_status(outcome)
        finalized = ledger.finalize(
            conn,
            result["reservation_id"],
            now_done,
            accepted=outcome.accepted,
            run_status=status,
            engine_code=outcome.code,
            engine_ref=outcome.engine_ref,
            result_json=outcome.result_json,
            detail=outcome.detail,
            usage=usage_from_json(outcome.result_json),
        )
        run = finalized.get("run") or ledger.read_run(conn, user["id"], result["run_id"])
        if as_json:
            return self._json(200, {"run": _public_run(run), "engine_blockers": outcome.blockers or []})
        return self._redirect(f"/app/runs/{run['id']}")

    def _cancel(self, conn, user, run_id, now_iso) -> Response:
        if not ID_RE.match(run_id):
            return self._json(404, {"error": "not_found"})
        result = ledger.cancel(conn, user["id"], run_id, now_iso)
        if result is None:
            return self._json(404, {"error": "not_found"})
        return self._json(200, {"run": _public_run(result["run"]), "released": result["released"]})

    def _checkout(self, conn, user, now_iso, *, as_json: bool) -> Response:
        status = payments.public_status(self.config)
        if not status["enabled"]:
            payload = {"error": "payments_disabled", "price": None, "provider": None, "collects_card_numbers": False}
            if as_json:
                return self._json(403, payload)
            return self._html(403, billing_page(user, ledger.snapshot(conn, user["id"], lock_unverified=self.config.require_verified_email), status))
        if self.config.stripe_secret_key and self.config.stripe_price_id:
            url = payments.create_checkout_session(
                self.config.stripe_secret_key,
                user["id"],
                self.config.stripe_price_id,
                self.config.canonical_origin,
            )
            if not url:
                return self._json(502, {"error": "stripe_unavailable", "price": None, "provider": "stripe"})
            if as_json:
                return self._json(200, {"ok": True, "provider": "stripe", "url": url, "price": None})
            return self._redirect(url)
        link = self.config.stripe_payment_link or ""
        separator = "&" if "?" in link else "?"
        target = f"{link}{separator}client_reference_id={quote(user['id'])}"
        if as_json:
            return self._json(200, {"ok": True, "provider": "stripe", "url": target, "price": None})
        return self._redirect(target)

    def _stripe_webhook(self, conn, headers, body, now_iso) -> Response:
        if not payments.stripe_webhook_configured(self.config):
            return self._json(503, {"error": "payments_disabled", "provider": None})
        decision = payments.parse_stripe_event(
            self.config,
            body,
            headers.get("stripe-signature", ""),
            int(utcnow().timestamp()),
        )
        if not decision.ok:
            return self._json(decision.status, {"error": decision.error})
        if decision.event_id is None:
            return self._json(200, {"ignored": True})
        account = conn.execute("SELECT user_id FROM ledger_accounts WHERE user_id = ?", (decision.user_id,)).fetchone()
        if account is None:
            return self._json(404, {"error": "unknown_account"})
        granted = ledger.grant_paid(
            conn,
            decision.user_id,
            decision.credits,
            decision.event_id,
            payments.payload_hash(body),
            now_iso,
        )
        return self._json(200, {"ok": True, "duplicate": granted["duplicate"], "credits": granted["credits"]})

    def _webhook(self, conn, headers, body, now, now_iso) -> Response:
        if not self.config.payments_ready():
            return self._json(403, {"error": "payments_disabled", "provider": None, "integration": "none"})
        decision = payments.parse_event(
            self.config,
            body,
            headers.get(payments.SIGNATURE_HEADER, ""),
            int(now.timestamp()),
        )
        if not decision.ok:
            return self._json(decision.status, {"error": decision.error})
        if decision.error == "ignored" or decision.event_id is None:
            return self._json(200, {"ignored": True})
        account = conn.execute("SELECT user_id FROM ledger_accounts WHERE user_id = ?", (decision.user_id,)).fetchone()
        if account is None:
            return self._json(404, {"error": "unknown_account"})
        granted = ledger.grant_paid(
            conn,
            decision.user_id,
            decision.credits,
            decision.event_id,
            payments.payload_hash(body),
            now_iso,
        )
        return self._json(200, {"ok": True, "duplicate": granted["duplicate"], "credits": granted["credits"]})

    def _dashboard(self, conn, user, error=None, question="", idempotency_key=None) -> str:
        view = ledger.snapshot(conn, user["id"], lock_unverified=self.config.require_verified_email)
        runs = ledger.list_runs(conn, user["id"])
        armed = engine.blockers(self.config)
        hard = [item for item in armed if item != "team_talk_absent"]
        return dashboard(
            user,
            view,
            runs,
            idempotency_key=idempotency_key or new_token()[:24],
            error=error,
            question=question,
            engine_armed=not hard,
            budget="" if self.config.max_run_budget_usd is None else str(self.config.max_run_budget_usd),
        )

    def _owned_run(self, conn, user, run_id: str):
        if not ID_RE.match(run_id):
            return None
        return ledger.read_run(conn, user["id"], run_id)

    def _user(self, conn, headers, now: datetime):
        token = _cookie(headers.get("cookie", ""), self.config.cookie_name)
        if not token:
            return None
        row = conn.execute(
            """
            SELECT s.token_hash, s.csrf, s.expires_at, u.id, u.email, u.role
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        if parse_iso(row["expires_at"]) <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            return None
        role = row["role"]
        return {
            "id": row["id"],
            "email": row["email"],
            "role": role,
            "csrf": row["csrf"],
            "token_hash": row["token_hash"],
        }

    def _rate(self, conn, bucket_name, material, now, kind="auth") -> bool:
        if kind == "signup":
            limit = self.config.signup_limit
            window = self.config.auth_window_seconds
        elif kind == "start":
            limit = self.config.start_limit
            window = self.config.start_window_seconds
        else:
            limit = self.config.auth_limit
            window = self.config.auth_window_seconds
        bucket = kind + ":" + client_bucket(self.config.secret, bucket_name + ":" + material)
        return analytics.allow(conn, bucket, int(now.timestamp()), window, limit)

    def _form_page(self, mode: str, status: int = 200, **kwargs) -> Response:
        token = new_csrf()
        response = self._html(status, auth_page(mode=mode, form_csrf=token, **kwargs))
        response.headers.append(("Set-Cookie", self._named_cookie(self.config.form_cookie_name, token, 3600)))
        return response

    def _form_ok(self, headers, form) -> bool:
        expected = _cookie(headers.get("cookie", ""), self.config.form_cookie_name) or ""
        sent = str(form.get("csrf", ""))
        if not expected or not sent or len(expected) != len(sent):
            return False
        return hmac.compare_digest(expected, sent)

    def _operator_ok(self, headers) -> bool:
        expected = self.config.operator_token or ""
        header = headers.get("authorization", "")
        prefix = "Bearer "
        if not expected or not header.startswith(prefix):
            return False
        token = header[len(prefix) :].strip()
        if len(token) != len(expected):
            return False
        return hmac.compare_digest(token, expected)

    def _operator(self, conn, path, form, now_iso) -> Response:
        if path == "/api/operator/promote":
            email = normalize_email(str(form.get("email", "")))
            row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone() if email else None
            if row is None:
                return self._json(404, {"error": "not_found"})
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
            return self._json(200, {"ok": True, "role": "admin"})
        if path == "/api/operator/verify":
            email = normalize_email(str(form.get("email", "")))
            row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone() if email else None
            if row is None:
                return self._json(404, {"error": "not_found"})
            conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (row["id"],))
            return self._json(200, {"ok": True, "email_verified": True})
        run_id = path[len("/api/operator/runs/") : -len("/resolve")]
        if not ID_RE.match(run_id):
            return self._json(404, {"error": "not_found"})
        action = str(form.get("action", ""))
        if action not in {"release", "commit"}:
            return self._json(400, {"error": "bad_action"})
        result = ledger.resolve_unknown(conn, run_id, now_iso, action)
        if result is None:
            return self._json(404, {"error": "not_found"})
        return self._json(200, {"changed": result["changed"], "run": _public_run(result["run"])})

    def _count(self, conn, now, user, path) -> None:
        if _is_asset(path):
            return
        analytics.record(
            conn,
            day=now.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            authed=bool(user),
            user_id=user["id"] if user else None,
            path_class=_path_class(path),
        )

    def _static(self, path: str) -> Response:
        relative = path.lstrip("/")
        if not relative or relative.startswith(BLOCKED_PREFIXES) or ".." in relative.split("/"):
            return self._html(404, not_found())
        root = self.config.public_root.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return self._html(404, not_found())
        if not candidate.is_file():
            return self._html(404, not_found())
        kind = STATIC_TYPES.get(candidate.suffix.lower())
        if kind is None:
            return self._html(404, not_found())
        data = candidate.read_bytes()
        cache = "no-cache" if candidate.suffix.lower() == ".html" else "public, max-age=3600"
        return Response(200, [("Content-Type", kind), ("Cache-Control", cache), ("X-Content-Type-Options", "nosniff")], data)

    def _named_cookie(self, name: str, token: str, max_age: int) -> str:
        secure = "; Secure" if self.config.cookie_secure else ""
        return f"{name}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}"

    def _cookie(self, token: str, max_age: int) -> str:
        return self._named_cookie(self.config.cookie_name, token, max_age)

    def _security(self, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        headers = list(headers)
        headers.extend(
            [
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                ("Referrer-Policy", "same-origin"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self'; font-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
                ),
                ("Cache-Control", "no-store"),
            ]
        )
        return headers

    def _html(self, status: int, html: str) -> Response:
        body = html.encode("utf-8")
        return Response(status, self._security([("Content-Type", "text/html; charset=utf-8")]), body)

    def _json(self, status: int, payload: dict) -> Response:
        body = json.dumps(payload).encode("utf-8")
        return Response(status, self._security([("Content-Type", "application/json; charset=utf-8")]), body)

    def _text(self, status: int, text: str) -> Response:
        return Response(status, self._security([("Content-Type", "text/plain; charset=utf-8")]), text.encode("utf-8"))

    def _redirect(self, location: str, *, clear_cookie: bool = False) -> Response:
        headers = [("Location", location)]
        if clear_cookie:
            headers.append(("Set-Cookie", f"{self.config.cookie_name}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"))
        return Response(303, self._security(headers), b"")

    def _reject_csrf(self, path, user) -> Response:
        if path.startswith("/api/"):
            return self._json(403, {"error": "bad_csrf"})
        return self._html(403, message_page("Form rejected", "Reload the page and submit it again.", user=user))


def _hostname(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 1 else ""
    return text.split(":", 1)[0].rstrip(".")


def client_identity(headers: dict, socket_peer: str, config: Config) -> str:
    """Rate limits use the socket peer. X-Forwarded-For is ignored unless that peer is trusted."""
    peer = socket_peer or "local"
    if peer not in config.trusted_proxies or config.trusted_proxy_hops <= 0:
        return peer
    parts = [item.strip() for item in headers.get("x-forwarded-for", "").split(",") if item.strip()]
    index = len(parts) - int(config.trusted_proxy_hops)
    if index < 0 or index >= len(parts):
        return peer
    candidate = parts[index]
    if len(candidate) > 80 or any(ord(ch) < 33 for ch in candidate):
        return peer
    return candidate


def _cookie(header: str, name: str) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return None
    morsel = jar.get(name)
    if morsel is None:
        return None
    return morsel.value


def _parse_body(headers: dict, body: bytes) -> dict:
    content = headers.get("content-type", "")
    if "application/json" in content:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): data[k] for k in data}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _csrf_ok(user: dict, headers: dict, form: dict) -> bool:
    sent = headers.get("x-csrf-token") or str(form.get("csrf", ""))
    return bool(sent) and sent == user["csrf"]


def _clean_question(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in cleaned):
        return None
    if not cleaned or len(cleaned) > 4000:
        return None
    return cleaned


def _safe_next(query: str) -> str:
    values = parse_qs(query)
    return _safe_next_value((values.get("next") or ["/app"])[-1])


def _safe_next_value(value: str) -> str:
    if not isinstance(value, str):
        return "/app"
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/app"
    if "://" in value or ".." in value:
        return "/app"
    return value


def _path_class(path: str) -> str:
    if path.startswith("/api/admin") or path.startswith("/admin"):
        return "admin"
    if path.startswith("/api"):
        return "api"
    if path.startswith("/app"):
        return "app"
    if path.startswith("/account"):
        return "account"
    if path in {"/", "/index.html", "/start", "/start.html", "/pricing.html", "/docs.html", "/example.html"}:
        return "marketing"
    return "unknown"


def _is_asset(path: str) -> bool:
    return path.startswith(("/assets/", "/css/", "/js/")) or path in {"/robots.txt", "/sitemap.xml"}


def _public_run(run: dict | None) -> dict | None:
    if not run:
        return None
    return {
        "id": run["id"],
        "status": run["status"],
        "question": run["question"],
        "engine_code": run.get("engine_code"),
        "engine_ref": run.get("engine_ref"),
        "detail": run.get("detail"),
        "created_at": run["created_at"],
        "updated_at": run.get("updated_at"),
        "has_report": _has_report(run),
    }


def _has_report(run: dict) -> bool:
    raw = run.get("result_json")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    findings = data.get("findings_confirmed")
    if isinstance(findings, list) and any(str(item).strip() for item in findings):
        return True
    report = data.get("report")
    if isinstance(report, str):
        return bool(report.strip())
    if isinstance(report, dict):
        nested = report.get("findings_confirmed") or []
        return any(str(item).strip() for item in nested)
    return False


def _public_runs(runs: list[dict]) -> list[dict]:
    return [_public_run(run) for run in runs]


def _run_status(outcome: engine.EngineOutcome) -> str:
    if outcome.code in {"not_counted", "finished_unconfirmed", "completed", "unknown", "retry_later", "issues_found"}:
        return outcome.code
    if outcome.disposition == "intake":
        return "intake"
    if not outcome.accepted:
        return "engine_unavailable"
    return "finished_unconfirmed"


def assert_no_seat_names(blob: str) -> bool:
    lowered = blob.lower()
    return not any(re.search(rf"\b{name}\b", lowered) for name in SEAT_NAMES)


def make_server(app: "App", port: int | None = None):
    """Start a daemon HTTP thread. Tests pass port 0. serve() uses the configured port."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    server_app = app
    bind_port = app.config.port if port is None else port

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._serve()

        def do_POST(self):
            self._serve()

        def do_HEAD(self):
            self._serve()

        def log_message(self, fmt, *args):
            return

        def _serve(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            path_only = self.path.split("?", 1)[0]
            limit = server_app.config.max_body_bytes
            if self.command == "POST" and path_only.rstrip("/") in {"/app/runs", "/api/runs"}:
                limit = server_app.config.max_upload_bytes + 80_000
            if length > limit:
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = server_app.handle(self.command, self.path, headers, body, self.client_address[0])
            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            if not any(key.lower() == "content-length" for key, _value in response.headers):
                self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

    httpd = ThreadingHTTPServer((app.config.host, bind_port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def serve(app: "App") -> None:
    import time

    httpd = make_server(app)
    host, port = httpd.server_address
    print(f"Listening on http://{host}:{port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd.server_close()
