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


def layout(title: str, main: str, *, user: dict | None = None, robots: str = "noindex", head_extra: str = "") -> str:
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
  {head_extra}
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


def privacy_copy(cap: int) -> str:
    return (
        "Don't send sensitive material. For now, send only a general question or code you are not worried about. "
        f"This server stores the text of the test after it is cut to {int(cap)} characters, the engine reply, "
        "your email, a password hash, and a hash of the session cookie. It does not store the uploaded file. "
        "It does not store a raw IP address. Rate limits keep a keyed hash of the client address, and page views keep a separate hash that changes each day. "
        "A test, a report, and those hashes stay in the account database until that database is deleted. There is no automatic expiry for them. "
        "The same text is sent to the investigation engine on this machine. This page does not control how long that engine keeps a copy."
    )


def start_page(
    user: dict,
    *,
    idempotency_key: str,
    error: list[str] | None = None,
    question: str = "",
    paste: str = "",
    cap: int = 12000,
) -> str:
    notice = privacy_copy(cap)
    main = f"""
    <div class="shell">
      <p class="kicker">Crash test</p>
      <h1>Drop anything you want crash-tested and we'll crash test it for you.</h1>
      <div class="notice"><p>{esc(notice)}</p></div>
      {errors(error or [])}
      <form class="card form" method="post" action="/app/runs" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
        <input type="hidden" name="idempotency_key" value="{esc(idempotency_key)}">
        <fieldset class="field">
          <legend>Optional intent</legend>
          <div class="choices">
            <label><input type="radio" name="intent" value="find"> Find anything wrong</label>
            <label><input type="radio" name="intent" value="improve"> Help me make this better</label>
            <label><input type="radio" name="intent" value="test"> Run a test on it</label>
          </div>
        </fieldset>
        <div class="field">
          <label for="question">Question</label>
          <textarea id="question" name="question" maxlength="12000" placeholder="What do you want crash-tested?">{esc(question)}</textarea>
        </div>
        <div class="field">
          <label for="paste">Or paste text</label>
          <textarea id="paste" name="paste" maxlength="200000" placeholder="Paste the writing or the code.">{esc(paste)}</textarea>
        </div>
        <div class="field">
          <label for="upload">Or upload a file</label>
          <input id="upload" name="upload" type="file" accept=".txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.html,.css,.zip">
          <p class="meta">Text, code, or a .zip of an app. 2 MB maximum. The file is not saved. Only text is sent, and it is cut to {int(cap)} characters.</p>
        </div>
        <label class="check">
          <input type="checkbox" name="privacy" value="yes" required>
          <span>I read the notice. This is not sensitive material. I understand what is stored.</span>
        </label>
        <button class="btn btn-primary" type="submit">Crash test it</button>
      </form>
      <p class="meta"><a href="/app">Your tests</a></p>
    </div>
    """
    return layout("Start a crash test — AI Crash Test Dummies", main, user=user)


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
          <p>The five included tests are used. Paid tests are coming soon. No price is published, and this page does not start another model call.</p>
          <p><a class="btn btn-primary" href="/app/billing">View payment status</a></p>
        </section>
        """
    else:
        if engine_armed:
            banner = (
                '<div class="banner"><p>Five crash tests are included with your account. A test counts when a provider call is recorded. The report stays on your account. Paid tests are coming soon, and no price is published.</p></div>'
            )
        else:
            banner = (
                '<div class="banner banner-warn"><p>The investigation service is not configured on this process, so a submit here does not call a model and does not write a report.</p></div>'
            )
        starter = f"""
        <section class="card" id="start">
          <h2>Start a crash test</h2>
          {banner}
          <p><a class="btn btn-primary" href="/start">Start here</a></p>
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
    running = run["status"] in {"reserved", "dispatching", "accepted", "in_progress", "retry_later"}
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
    if running:
        status_line = f'<p class="status-live">{esc(label)}</p><p>This test is still running. This page checks again every few seconds. One test is held against the five included tests.</p>'
        refresh = '<meta http-equiv="refresh" content="5">'
    else:
        status_line = f'<p class="status-live">{esc(label)}</p><p>{esc(detail or "The run has a final status.")}</p>'
        refresh = ""
    main = f"""
    <div class="shell">
      <p class="kicker">Crash test</p>
      <h1>Report</h1>
      <div class="banner {'banner-warn' if run['status'] in {'engine_unavailable', 'released', 'cancelled', 'intake', 'unknown', 'not_counted'} else ''}">
        {status_line}
      </div>
      <dl class="status-dl card">
        <div><dt>Status</dt><dd>{esc(label)}</dd></div>
        <div><dt>Credit</dt><dd>{esc(credit)}</dd></div>
        {ref}
        <div><dt>Opened</dt><dd><time datetime="{esc(run['created_at'])}">{esc(run['created_at'])}</time></dd></div>
        <div><dt>What was sent</dt><dd class="question">{esc(run['question'])}</dd></div>
      </dl>
      <section class="card">
        <h2>Report</h2>
        {result}
        <p class="meta">This page only shows what came back for this account.</p>
      </section>
      <p><a class="btn btn-ghost" href="/app">Back to your tests</a></p>
    </div>
    """
    return layout("Crash test — AI Crash Test Dummies", main, user=user, head_extra=refresh)


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
    if payment.get("enabled"):
        offer = (
            f'<p>Stripe is configured. A paid return adds {int(payment.get("credits") or 0)} crash tests after the webhook confirms payment. No card number is collected here, and no price is printed on this page.</p>'
            f'<form method="post" action="/app/billing/checkout">'
            f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">'
            f'<button class="btn btn-primary" type="submit">Continue to Stripe</button></form>'
        )
    else:
        offer = """
        <p>Paid tests coming soon. No price is published, and this repository has no model rate card. Checkout cannot run.</p>
        <button class="btn btn-primary" type="button" disabled>Checkout unavailable</button>
        """
    main = f"""
    <div class="shell">
      <p class="kicker">Payment</p>
      <h1>Additional tests</h1>
      <p class="lede">Five crash tests are included with an account. Paid tests are coming soon. No price is published.</p>
      {_allowance(view)}
      <section class="card">
        <h2>Checkout is {'on' if payment.get('enabled') else 'off'}</h2>
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


def operator_login_page(error: str) -> str:
    notice = f'<p class="meta">{esc(error)}</p>' if error else ""
    main = f"""
    <div class="shell">
      <p class="kicker">Operator</p>
      <h1>Visitor notes</h1>
      <p class="lede">This page is not an account role. It opens with the operator token configured on the server.</p>
      {notice}
      <form method="post" action="/admin/operator-login" class="stack">
        <label>Operator token <input type="password" name="token" autocomplete="off" required></label>
        <button class="btn btn-primary" type="submit">Open</button>
      </form>
    </div>
    """
    return layout("Visitors — AI Crash Test Dummies", main)


def visitors_dashboard(summary: dict) -> str:
    def cell(value) -> str:
        return esc(str(value or 0))

    day_rows = []
    for day in summary.get("days") or []:
        day_rows.append(
            "<tr>"
            f"<td>{esc(str(day.get('day') or ''))}</td>"
            f"<td>{cell(day.get('human_views'))}</td><td>{cell(day.get('human_uniques'))}</td>"
            f"<td>{cell(day.get('bot_views'))}</td><td>{cell(day.get('bot_uniques'))}</td>"
            f"<td>{cell(day.get('unknown_views'))}</td><td>{cell(day.get('unknown_uniques'))}</td>"
            "</tr>"
        )
    if not day_rows:
        day_rows.append("<tr><td colspan='7'>None recorded</td></tr>")

    def pairs(items, left, right) -> str:
        rows = [
            f"<tr><td>{esc(str(item.get(left) or ''))}</td><td>{cell(item.get(right))}</td></tr>"
            for item in items
        ]
        return "".join(rows) or "<tr><td colspan='2'>None recorded</td></tr>"

    main = f"""
    <div class="shell">
      <p class="kicker">Operator</p>
      <h1>Visitors</h1>
      <p class="lede">Daily uniques and page views, split human, bot, and unknown. The split is best-effort. A browser that never moves stays unknown. Raw IP addresses are not stored.</p>
      <section class="card">
        <h2>Last 14 days</h2>
        <table>
          <thead><tr><th>Day</th><th>Human views</th><th>Human uniques</th><th>Bot views</th><th>Bot uniques</th><th>Unknown views</th><th>Unknown uniques</th></tr></thead>
          <tbody>{''.join(day_rows)}</tbody>
        </table>
      </section>
      <section class="card">
        <h2>Top pages</h2>
        <table><thead><tr><th>Path</th><th>Views</th></tr></thead><tbody>{pairs(summary.get('pages') or [], 'path', 'views')}</tbody></table>
      </section>
      <section class="card">
        <h2>Top referrers</h2>
        <table><thead><tr><th>Referrer</th><th>Views</th></tr></thead><tbody>{pairs(summary.get('referrers') or [], 'referrer', 'views')}</tbody></table>
      </section>
      <section class="card">
        <h2>Signups</h2>
        <table><thead><tr><th>Day</th><th>Accounts</th></tr></thead><tbody>{pairs(summary.get('signups') or [], 'day', 'n')}</tbody></table>
      </section>
      <section class="card">
        <h2>Crash tests</h2>
        <table><thead><tr><th>Day</th><th>Runs</th></tr></thead><tbody>{pairs(summary.get('crash_tests') or [], 'day', 'n')}</tbody></table>
      </section>
      <form method="post" action="/admin/operator-logout"><button class="btn btn-ghost" type="submit">Close</button></form>
    </div>
    """
    return layout("Visitors — AI Crash Test Dummies", main)
