"""Server-rendered account pages. Same brand, no invented findings."""

from __future__ import annotations

import json
import re
from html import escape

BRAND_SVG = (
    '<svg viewBox="0 0 48 48" aria-hidden="true">'
    '<path fill="currentColor" d="M24 4 44 40H4L24 4Zm0 10.2L12.6 36h22.8L24 14.2Z"/></svg>'
)

STATUS_LABEL = {
    "reserved": "Credit held",
    "engine_unavailable": "Not sent",
    "intake": "Opened, model not called",
    "unknown": "Outcome unknown",
    "not_counted": "Not counted",
    "finished_unconfirmed": "Finished, nothing confirmed",
    "retry_later": "Not started",
    "dispatching": "Sending",
    "accepted": "Accepted, no report",
    "in_progress": "In progress",
    "completed": "Payload received",
    "failed": "Failed after acceptance",
    "cancelled": "Cancelled",
    "released": "Credit released",
}


def esc(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def layout(title: str, main: str, *, user: dict | None = None, robots: str = "noindex") -> str:
    if user:
        account = (
            f'<span class="who">{esc(user["email"])}</span>'
            f'<a class="btn btn-ghost" href="/app">Account</a>'
            f'<form method="post" action="/account/logout" class="inline-form">'
            f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
            f'<button class="btn btn-ghost" type="submit">Log out</button></form>'
        )
        mobile = (
            f'<a class="nav-cta" href="/app">Account</a>'
            f'<form method="post" action="/account/logout" class="inline-form">'
            f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
            f'<button class="nav-cta btn btn-ghost" type="submit">Log out</button></form>'
        )
    else:
        account = (
            '<a class="btn btn-ghost" href="/account/login">Sign in</a>'
            '<a class="btn btn-primary" href="/app">Start a Crash Test</a>'
        )
        mobile = (
            '<a class="nav-cta" href="/account/login">Sign in</a>'
            '<a class="nav-cta btn btn-primary" href="/app">Start a Crash Test</a>'
        )
    admin = '<a href="/admin">Admin</a>' if user and user.get("role") == "admin" else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <meta name="robots" content="{esc(robots)}">
  <link rel="icon" href="/assets/mark.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/css/site.css">
</head>
<body class="app-body">
  <a class="skip" href="#main">Skip to content</a>
  <header class="site-header">
    <div class="wrap bar">
      <a class="brand" href="/">
        {BRAND_SVG}
        <span><strong>AI CRASH TEST DUMMIES</strong><span>ADVERSARIAL AI INVESTIGATION SYSTEM</span></span>
      </a>
      <button class="nav-toggle" type="button" aria-expanded="false" aria-controls="site-nav" aria-label="Menu"><span></span><span></span><span></span></button>
      <nav id="site-nav" class="nav" aria-label="Primary">
        <a href="/#how">How it works</a>
        <a href="/pricing.html">Pricing</a>
        <a href="/docs.html">Docs</a>
        {admin}
        {mobile}
      </nav>
      <div class="actions">{account}</div>
    </div>
  </header>
  <main id="main" class="page">{main}</main>
  <footer class="site-footer">
    <div class="wrap foot">
      <a class="brand" href="/">
        {BRAND_SVG}
        <span><strong>AI CRASH TEST DUMMIES</strong><span>ADVERSARIAL AI INVESTIGATION SYSTEM</span></span>
      </a>
      <nav aria-label="Footer">
        <a href="/docs.html#privacy">Privacy</a>
        <a href="/docs.html#terms">Terms</a>
        <a href="/pricing.html">Pricing</a>
        <a href="/docs.html">Docs</a>
      </nav>
      <p class="slogan">Investigate.<br>Validate.<br>Advance.</p>
    </div>
  </footer>
  <script src="/js/site.js"></script>
</body>
</html>
"""


def errors(items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{esc(item)}</li>" for item in items)
    return f'<div class="banner banner-bad" role="alert"><ul class="plain">{lis}</ul></div>'


def auth_page(*, mode: str, form_csrf: str, error: list[str] | None = None, email: str = "", next_path: str = "/app") -> str:
    login = mode == "login"
    title = "Sign in" if login else "Create an account"
    action = "/account/login" if login else "/account/signup"
    password_auto = "current-password" if login else "new-password"
    submit = "Sign in" if login else "Create account"
    switch = (
        '<p class="meta">No account yet. <a href="/account/signup">Create one</a>.</p>'
        if login
        else '<p class="meta">Already registered. <a href="/account/login">Sign in</a>.</p>'
    )
    intro = (
        "Sign in to see your tests and the five included credits."
        if login
        else "An account is how this site knows a test is yours. Five crash tests are included. A price for more has not been set."
    )
    main = f"""
    <div class="shell">
      <p class="kicker">Account</p>
      <h1>{esc(title)}</h1>
      <p class="lede">{esc(intro)}</p>
      {errors(error or [])}
      <form class="card form" method="post" action="{action}" novalidate>
        <input type="hidden" name="csrf" value="{esc(form_csrf)}">
        <input type="hidden" name="next" value="{esc(next_path)}">
        <div class="field">
          <label for="email">Email</label>
          <input id="email" name="email" type="email" autocomplete="username" required maxlength="254" value="{esc(email)}">
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" name="password" type="password" autocomplete="{password_auto}" required minlength="12" maxlength="200">
        </div>
        {"" if login else '''<div class="field">
          <label for="confirm">Confirm password</label>
          <input id="confirm" name="confirm" type="password" autocomplete="new-password" required minlength="12" maxlength="200">
        </div>'''}
        <button class="btn btn-primary btn-lg" type="submit">{submit}</button>
      </form>
      {switch}
    </div>
    """
    return layout(f"{title} — AI Crash Test Dummies", main)


def _allowance(view: dict) -> str:
    remaining = view["remaining"]
    noun = "test" if remaining == 1 else "tests"
    return f"""
    <section class="allowance" aria-label="Included tests">
      <div>
        <p class="kicker">Included</p>
        <p class="allowance-count"><b>{int(view["free_remaining"])}</b><span>of {int(view["free_allowance"])} included {noun} remaining</span></p>
      </div>
      <dl class="allowance-meta">
        <div><dt>Held</dt><dd>{int(view["free_held"] + view["paid_held"])}</dd></div>
        <div><dt>Used</dt><dd>{int(view["free_committed"] + view["paid_committed"])}</dd></div>
        <div><dt>Paid left</dt><dd>{int(view["paid_remaining"])}</dd></div>
      </dl>
    </section>
    """


def _ledger(view: dict) -> str:
    if not view["entries"]:
        return '<p class="meta">No credits have moved yet.</p>'
    items = []
    for entry in view["entries"]:
        items.append(
            f'<li><span>{esc(entry["note"])}</span><time datetime="{esc(entry["created_at"])}">{esc(entry["created_at"])}</time></li>'
        )
    return '<ul class="ledger">' + "".join(items) + "</ul>"


def _runs(runs: list[dict]) -> str:
    if not runs:
        return '<p class="meta">No crash tests yet. A start stays on this account. It is not visible to anyone else.</p>'
    items = []
    for run in runs:
        label = STATUS_LABEL.get(run["status"], run["status"])
        question = run["question"].replace("\n", " ")
        if len(question) > 90:
            question = question[:87] + "…"
        items.append(
            f'<li><a href="/app/runs/{esc(run["id"])}"><span class="question">{esc(question)}</span>'
            f'<span class="pill">{esc(label)}</span></a></li>'
        )
    return '<ul class="run-list">' + "".join(items) + "</ul>"


def dashboard(
    user: dict,
    view: dict,
    runs: list[dict],
    *,
    idempotency_key: str,
    error: list[str] | None = None,
    question: str = "",
    engine_armed: bool = False,
    budget: str = "",
) -> str:
    blocked = view["remaining"] <= 0
    held = view["free_held"] + view["paid_held"]
    if view.get("credits_locked"):
        starter = """
        <section class="card" id="start">
          <h2>Included tests are locked</h2>
          <p>This account has not confirmed email ownership. No test is sent, and the five included credits stay unused.</p>
        </section>
        """
    elif blocked and held:
        starter = """
        <section class="card" id="start">
          <h2>A test is still unresolved</h2>
          <p>One or more credits are held. A lost engine reply stays held so it cannot be spent again. Nothing else is sent until that hold is resolved.</p>
        </section>
        """
    elif blocked:
        starter = """
        <section class="card" id="start">
          <h2>Payment required before another test</h2>
          <p>The included tests are used, and there is no paid credit on this account. Nothing is sent to a model from this page.</p>
          <p><a class="btn btn-primary" href="/app/billing">View payment status</a></p>
        </section>
        """
    else:
        if engine_armed:
            banner = (
                '<div class="banner"><p>The engine binding is armed. A credit is counted only if the engine accepts the run. '
                'If the reply is lost, the credit stays held. The budget has to be enforced by the engine, not by a number in this form.</p></div>'
            )
        else:
            banner = (
                '<div class="banner banner-warn"><p>The investigation engine is not connected on this host. '
                'Submitting holds a credit, finds no engine acceptance, and releases the credit. No report is written.</p></div>'
            )
        starter = f"""
        <section class="card" id="start">
          <h2>Start a crash test</h2>
          {banner}
          {errors(error or [])}
          <form class="form" method="post" action="/app/runs">
            <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
            <input type="hidden" name="idempotency_key" value="{esc(idempotency_key)}">
            <div class="field">
              <label for="question">Question</label>
              <textarea id="question" name="question" required maxlength="4000" placeholder="State the claim or the code question.">{esc(question)}</textarea>
            </div>
            <button class="btn btn-primary" type="submit">Submit test</button>
          </form>
        </section>
        """
    lock = ""
    if view.get("credits_locked"):
        lock = (
            '<div class="banner banner-warn"><p>Email ownership is not confirmed, so the five included tests stay locked. '
            "No mail provider is configured, and this server does not send mail.</p></div>"
        )
    main = f"""
    <div class="shell">
      <p class="kicker">Your account</p>
      <h1>Crash tests</h1>
      <p class="lede">Five tests are included. A test counts when a provider call is recorded. An uncertain reply stays held.</p>
      {lock}
      {_allowance(view)}
      {starter}
      <section class="card">
        <h2>Recent tests</h2>
        {_runs(runs)}
      </section>
      <p class="meta"><a href="/app/billing">Payment status</a></p>
      <section class="card">
        <h2>Credit ledger</h2>
        <p class="meta">Holds are not charges. A released row means the credit came back.</p>
        {_ledger(view)}
      </section>
    </div>
    """
    return layout("Your crash tests — AI Crash Test Dummies", main, user=user)


def run_page(user: dict, run: dict) -> str:
    label = STATUS_LABEL.get(run["status"], run["status"])
    credit = "Released back to the account."
    if run["status"] == "intake":
        credit = "Not counted. Opening a campaign records seats and does not call a model."
    elif run["status"] == "not_counted":
        credit = "Not counted. No provider call was recorded."
    elif run["status"] == "finished_unconfirmed":
        credit = "Counted. A provider call finished. No finding was confirmed."
    elif run["status"] == "unknown":
        credit = "Held for operator review. This run is not retried."
    elif run["status"] == "dispatching":
        credit = "Held while the request is in flight."
    elif run["status"] in {"accepted", "completed", "failed"}:
        credit = "Counted. The engine had already accepted the run."
    elif run["status"] == "reserved":
        credit = "Held. Not counted yet."
    elif run["status"] == "cancelled":
        credit = "Released. Cancelled before acceptance."
    detail = run.get("detail") or ""
    result = _result_block(run.get("result_json"))
    ref = ""
    if run.get("engine_ref"):
        ref = f'<div><dt>Engine reference</dt><dd><code>{esc(run["engine_ref"])}</code></dd></div>'
    main = f"""
    <div class="shell">
      <p class="kicker">Crash test</p>
      <h1>{esc(label)}</h1>
      <div class="banner {'banner-warn' if run['status'] in {'engine_unavailable', 'released', 'cancelled', 'intake', 'unknown', 'not_counted'} else ''}">
        <p>{esc(detail or 'No engine detail was recorded.')}</p>
      </div>
      <dl class="status-dl card">
        <div><dt>Status</dt><dd>{esc(label)}</dd></div>
        <div><dt>Credit</dt><dd>{esc(credit)}</dd></div>
        {ref}
        <div><dt>Opened</dt><dd><time datetime="{esc(run['created_at'])}">{esc(run['created_at'])}</time></dd></div>
        <div><dt>Question</dt><dd class="question">{esc(run['question'])}</dd></div>
      </dl>
      <section class="card">
        <h2>Report</h2>
        {result}
        <p class="meta">Marketing examples are not this test. This page only shows what the engine returned for this account.</p>
      </section>
      <p><a class="btn btn-ghost" href="/app">Back to your tests</a></p>
    </div>
    """
    return layout("Crash test — AI Crash Test Dummies", main, user=user)


def _result_block(result_json: str | None) -> str:
    if not result_json:
        return '<p>No report was produced.</p>'
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return '<p>The engine returned data this page will not treat as a report.</p>'
    if not isinstance(data, dict):
        return '<p>The engine returned data this page will not treat as a report.</p>'
    if "crash_result" in data or "findings_confirmed" in data or data.get("error_code") == "cost_policy_absent":
        findings = data.get("findings_confirmed") if isinstance(data.get("findings_confirmed"), list) else []
        shown = [str(item) for item in findings if str(item).strip()]
        parts = [
            f"<p>Crash result: {esc(data.get('crash_result') or 'not_confirmed')}.</p>",
            "<p>No finding was confirmed.</p>" if not shown else "<ul class=\"plain\">"
            + "".join(f"<li>{esc(item)}</li>" for item in shown)
            + "</ul>",
        ]
        if data.get("error_code") == "cost_policy_absent":
            parts.append("<p>No provider was called.</p>")
        raw = None
        report = data.get("report") if isinstance(data.get("report"), dict) else {}
        if isinstance(report.get("raw_model_text_untrusted"), str):
            raw = report["raw_model_text_untrusted"]
        elif isinstance(data.get("raw_model_text_untrusted"), str):
            raw = data["raw_model_text_untrusted"]
        if raw and raw.strip():
            parts.append("<p class=\"meta\">Untrusted model text. It is not a finding.</p>")
            parts.append(f"<pre class=\"question\">{esc(_scrub_seats(raw))}</pre>")
        return "".join(parts)
    if isinstance(data.get("report"), str) and data["report"].strip():
        return (
            '<p class="meta">Text below is the engine payload, shown as text. It is not a finding written by this website.</p>'
            f'<pre class="question">{esc(_scrub_seats(data["report"]))}</pre>'
        )
    return '<p>The engine did not return a confirmed finding. None was added here.</p>'


def _scrub_seats(text: str) -> str:
    return re.sub(
        r"\b(claude|chatgpt|grok|gemini|deepseek|flint)\b",
        "[redacted]",
        text,
        flags=re.IGNORECASE,
    )


def billing_page(user: dict, view: dict, payment: dict) -> str:
    if payment.get("enabled") and payment.get("price"):
        price = payment["price"]
        offer = (
            f'<p>Configured pack: {int(price["credits"])} tests, '
            f'{int(price["cents"])} {esc(price["currency"])} cents. '
            f'Provider: {esc(payment.get("provider"))}. This server still does not collect a card number.</p>'
            f'<form method="post" action="/app/billing/checkout">'
            f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
            f'<button class="btn btn-primary" type="submit">Create checkout record</button></form>'
        )
    else:
        offer = """
        <p>No customer price is configured, and this repository has no model rate card. Checkout cannot run, and this page will not invent a number or an API rate.</p>
        <button class="btn btn-primary" type="button" disabled>Checkout unavailable</button>
        """
    main = f"""
    <div class="shell">
      <p class="kicker">Payment</p>
      <h1>Additional tests</h1>
      <p class="lede">Five tests are included. A further test needs a customer price that has been chosen and a model cost policy taken from the real API rates. Neither is on file. This page does not sell a crash test.</p>
      {_allowance(view)}
      <section class="card">
        <h2>Checkout is {('on' if payment.get('enabled') else 'off')}</h2>
        {offer}
        {'<p class="meta">Card numbers are not collected on this site, and this server does not send a charge.</p>' if payment.get('enabled') else ''}
      </section>
      <p><a class="btn btn-ghost" href="/app">Back to your tests</a></p>
    </div>
    """
    return layout("Payment — AI Crash Test Dummies", main, user=user)


def admin_page(user: dict, stats: dict, gates: dict | None = None) -> str:
    gates = gates or {}
    rows = []
    for account in stats["accounts_detail"]:
        rows.append(
            "<tr>"
            f"<td>{esc(account['email'])}</td>"
            f"<td>{esc(account['role'])}</td>"
            f"<td>{int(account['free_committed'])} / {int(account['free_allowance'])}</td>"
            f"<td>{int(account['paid_committed'])} / {int(account['paid_credits'])}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="4">No accounts.</td></tr>'
    main = f"""
    <div class="shell shell-wide">
      <p class="kicker">Admin</p>
      <h1>Usage</h1>
      <p class="lede">Requests are not people. This database starts empty. No earlier traffic log was imported, and a request is not an account.</p>
      <dl class="status-dl card">
        <div><dt>Payments</dt><dd>{esc(gates.get('payments', 'disabled'))}</dd></div>
        <div><dt>Customer price</dt><dd>{esc(gates.get('customer_price', 'not set'))}</dd></div>
        <div><dt>Model cost policy</dt><dd>{esc(gates.get('model_cost_policy', 'missing'))}</dd></div>
        <div><dt>Engine</dt><dd>{esc(gates.get('engine', 'unavailable'))}</dd></div>
      </dl>
      <dl class="metric-grid">
        <div><dt>Anonymous requests</dt><dd>{int(stats['anonymous_requests'])}</dd><p>Page and API requests with no session.</p></div>
        <div><dt>Authenticated requests</dt><dd>{int(stats['authenticated_requests'])}</dd><p>Requests that carried a session. One person can make many.</p></div>
        <div><dt>Accounts</dt><dd>{int(stats['accounts'])}</dd><p>Registered accounts.</p></div>
        <div><dt>Unique signed-in accounts</dt><dd>{int(stats['unique_signed_in_accounts'])}</dd><p>Distinct accounts with at least one authenticated request.</p></div>
        <div><dt>Test attempts</dt><dd>{int(stats['test_attempts'])}</dd><p>Submits stored on an account, including ones the engine never accepted.</p></div>
        <div><dt>Engine accepted</dt><dd>{int(stats['engine_accepted_runs'])}</dd><p>Runs the engine accepted.</p></div>
        <div><dt>Reports received</dt><dd>{int(stats['reports_received'])}</dd><p>Accepted runs that included a report payload.</p></div>
      </dl>
      <section class="card">
        <h2>Accounts</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Email</th><th>Role</th><th>Included used</th><th>Paid used</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>
      </section>
    </div>
    """
    return layout("Usage — AI Crash Test Dummies", main, user=user)


def message_page(title: str, lede: str, *, user: dict | None = None, status_banner: str = "") -> str:
    banner = f'<div class="banner banner-warn"><p>{esc(status_banner)}</p></div>' if status_banner else ""
    main = f"""
    <div class="shell">
      <p class="kicker">AI Crash Test Dummies</p>
      <h1>{esc(title)}</h1>
      {banner}
      <p class="lede">{esc(lede)}</p>
      <p><a class="btn btn-primary" href="/">Back to the front door</a></p>
    </div>
    """
    return layout(f"{title} — AI Crash Test Dummies", main, user=user)


def not_found() -> str:
    return message_page(
        "Page not found",
        "That path is not on this site. The homepage was not substituted for it.",
    )
