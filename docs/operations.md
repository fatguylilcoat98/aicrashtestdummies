# Operations

This is the account shell in front of Team Talk. It is a Python 3.12 process with a SQLite file. It is not a Cloudflare Pages function, and it does not call a model unless every engine gate below is set.

## Run locally

```bash
export ACTD_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ACTD_ADMIN_EMAILS="you@example.com"
export ACTD_HOST=127.0.0.1
export ACTD_PORT=8080
export ACTD_DATA_DIR="$PWD/data"
python -m app
```

Open `http://127.0.0.1:8080/`. Create the admin account with the email in `ACTD_ADMIN_EMAILS`. The process prints nothing but a listen line. If `ACTD_SESSION_SECRET` is missing outside production, sessions die on restart and the secret is not printed.

Tests:

```bash
python -m unittest discover -s tests -v
```

## Decisions already made

- Five included tests per account. The number is fixed in code. `ACTD_FREE_CREDITS` is ignored.
- A credit is consumed when the engine returns an acceptance id, or when an operator resolves an unknown send as consumed. Acceptance is not a report.
- A credit stays held if the request was dispatched and the reply was lost, timed out, or incomplete. That hold still counts against the five, so a retry cannot start another model call. The same idempotency key does not send again.
- A credit is released only when the request never left this server, the engine returned a complete rejection, the owner cancels before dispatch, or an operator records that an unknown send did not run.
- Signing up with an address listed in `ACTD_ADMIN_EMAILS` does not create an admin. Email is not verified. Admin promotion and email verification require `ACTD_OPERATOR_TOKEN` on the server. Public launch stays closed while mail verification is missing.
- `X-Forwarded-For` is ignored unless the socket peer is listed in `ACTD_TRUSTED_PROXIES`.
- Checkout stays off unless live mode, a price, a provider, a webhook secret, a credit pack, a model-cost file, and `ACTD_ALLOW_LIVE_CHARGES` are all set. A local signature helper is not a payment provider. This build does not call a payment network.
- The same idempotency key always returns the same run. A new attempt needs a new key.
- A `reserved` hold that never started transmission expires after `ACTD_RESERVATION_TTL_SECONDS` (default 900) and is released. A `dispatching` or `unknown` hold does not expire into a release.
- Cancelling before acceptance releases the credit. Cancelling after acceptance does not refund it.
- Card numbers are never accepted. Checkout does nothing until provider, webhook secret, currency, price, and credit pack are all set. `fixture` mode is local and is refused when `ACTD_ENV=production`. Live mode also requires `ACTD_ALLOW_LIVE_CHARGES=1`, and this build still does not call Stripe or any other payment network.
- No price was found in the repo, so none is shown.
- Request counters are not people and do not include a historic visitor number.

## Team Talk adapter

The shell calls `POST http://127.0.0.1:5001/api/aict/v1/runs` with `Authorization: Bearer` from `AICTD_BOUNDARY_TOKEN` and `X-AICT-Owner` taken from the server session. It does not call `/api/lab/crash/open`. HTTP 200 is a terminal record, not a passed test. A free test is counted only when `model_invoked` is true on a terminal record other than `uncertain`. `cost_policy_absent` and a rejected input do not count. `uncertain` stays held and is not retried.

The live engine still needs `AICTD_COST_POLICY_PATH` with real non-zero rates and one saved participant before a provider call can happen. Until then every live run fails `cost_policy_absent` and the shell does not spend a credit. No price ships in source. Checkout stays off.

This shell does not call `POST /api/chat`, `POST /api/lab/crash/{id}/act`, experiment `authorize`/`execute`, or `POST /api/lab/bridge/crash-test`. Those can spend provider calls or run instruments, and Team Talk has no per-customer budget the shell can set first. A local `budget_usd` field is not sent.

The public tunnel `teamtalk.thegoodneighborguard.com` forwards to `localhost:5001` and the OpenAPI has no security scheme. GETs of `/openapi.json`, `/api/version`, and a missing campaign returned 200/404 without credentials. This shell refuses to POST unless the base URL host is loopback, so it does not use that tunnel. Chris's direct use of `127.0.0.1:5001` is unchanged. Closing the public bypass is an engine-side change in `https://github.com/fatguylilcoat98/Team-talk` at `2863a841239d50673947dc3f4876e52fa870fc1d` (dirty worktree, do not edit on the Dell): if `TEAM_TALK_SHELL_TOKEN` is set, require `Authorization: Bearer` only when the Host is `teamtalk.thegoodneighborguard.com` and the method/path is one of the billable routes above. Leave localhost and GET routes alone.

Run the account shell from a new directory such as `/home/chris/aictd-account-shell`, bound to `127.0.0.1:8765`, with its own SQLite file. Do not use port 8777. Do not change DNS. The apex stays on Pages.

A loopback POST to `/api/aict/v1/runs` is allowed only when all of these are true:

1. `ACTD_ENGINE_INVOCATION_ENABLED=1`
2. `ACTD_ENGINE_BASE_URL` is `http://127.0.0.1:<port>` or localhost
3. `ACTD_MAX_RUN_BUDGET_USD` is a positive number recorded by the operator. It is not sent to Team Talk.
4. `AICTD_BOUNDARY_TOKEN` is set in the shell environment. It is not shown in the browser.

The body is plain text. The shell does not send git, zip, local paths, or an owner id supplied by the browser. The idempotency key sent to the engine is a stable hash of the shell key.

## Payment gate

Leave these unset until Chris chooses them. There is no reliable existing price.

- `ACTD_PAYMENTS_MODE` — `disabled` (default), `fixture`, or `live`
- `ACTD_PAYMENT_PROVIDER` — unset. Stripe was not present. If Stripe is chosen later, use hosted checkout. Do not collect card numbers.
- `ACTD_PAYMENT_WEBHOOK_SECRET`
- `ACTD_PRICE_CENTS`
- `ACTD_PRICE_CURRENCY`
- `ACTD_CREDIT_PACK` — webhook `credits` must equal this or the event is rejected
- `ACTD_MODEL_COST_POLICY` — path to a file the operator writes from the real API rate card. This repo does not contain that file. Live checkout stays closed without it, even if a customer price is present.
- `ACTD_ALLOW_LIVE_CHARGES` — leave unset. This build will not place a live charge even if it is set.

Webhook header: `X-Actd-Signature: t=<unix>,v1=<hex hmac sha256 of "{t}.{raw body}">`. Replay of the same event id does not grant twice. Events received while payments are disabled are rejected and not stored.

## Production config still required

- `ACTD_ENV=production`
- `ACTD_SESSION_SECRET` long random value, only in the environment
- `ACTD_COOKIE_SECURE=1` behind HTTPS
- `ACTD_ADMIN_EMAILS`
- a persistent disk for `ACTD_DATA_DIR`
- a TLS reverse proxy
- the engine and payment decisions above, if those features should leave the fail-closed state

`/api/health` reports `engine` and `payments` without secrets.

## Hosting facts verified 2026-11-05

Read-only Cloudflare discovery, reported by the parent. This VM did not call Cloudflare. `/workspace/out/aict` was not present here, so nothing below was copied out of an export file.

- `aicrashtestdummies.com` is an active Free zone.
- The apex is a proxied CNAME to `aicrashtestdummies.pages.dev`.
- `www` and the wildcard currently target `uixie.porkbun.com`.
- The zone has no Workers routes and no Page Rules.
- The Pages project, branch, commit, and bindings could not be read. Pages Read was denied. Do not invent a production branch or a Pages project name.
- Analytics Read was denied, and the Web Analytics inventory was denied (Account Settings Read). Traffic volume and the human/bot split are unknown. Do not treat any earlier view claim as a measurement, and do not count historic requests as people.
- Cloudflare authorization on the parent is not permission for this branch to deploy. This process does not change DNS.

Routing issue, left unchanged: the apex and `www`/wildcard do not point at the same origin. `www.aicrashtestdummies.com` and other subdomains are not the Pages hostname the apex uses. Change that only after the intended mapping is verified.

## Single origin

The product is one site: `https://aicrashtestdummies.com`. The shell serves the marketing pages, signup, the crash-test form at `/start`, reports, checkout, and `/admin/visitors`. `app.aicrashtestdummies.com` and `www.aicrashtestdummies.com` 301 to the same path on `ACTD_CANONICAL_ORIGIN` (default `https://aicrashtestdummies.com`) when the request `Host` is in `ACTD_REDIRECT_HOSTS`.

Leave `ACTD_REDIRECT_HOSTS=-` on the current Dell shell until the apex DNS record points at the tunnel. If the shell redirects `app.` to the apex while Pages still serves the apex, account links and the shell redirect loop.

Cookies are host-only (`Path=/`, no `Domain`). They are set by the apex after the redirect, not by the alias host. Stripe success and cancel URLs use the canonical origin. The webhook is `https://aicrashtestdummies.com/api/billing/stripe-webhook`. The visitor beacon is same-origin `GET /v/hit`.

## Pages, until the DNS cutover

Merging this branch can publish the static site only. `python3 scripts/export_pages.py` writes `dist/` with HTML, CSS, JS, assets, `404.html`, `robots.txt`, `sitemap.xml`, `favicon.ico`, and `_redirects`. It copies no Python. Set the Pages build command to `python3 scripts/export_pages.py` and the output directory to `dist`. `wrangler.toml` names that output directory. `.assetsignore` is a backup if the dashboard build command is still empty and Pages publishes the repo root. An empty build command publishes the root, and `.assetsignore` is not a guarantee.

`_redirects` sends `/account/*` and `/app*` to the same path on `https://app.aicrashtestdummies.com` so those buttons work while Pages still owns the apex. That file is out of the request path once the apex CNAME points at the tunnel. Do not turn shell host redirects on while those Pages rules are still live.

## Cutover on the Dell

Do not deploy from this VM. Copy this branch into `/home/chris/aictd-account-shell` (not `/opt/team-talk`). Leave the dirty website checkout alone.

Shell environment:

- `ACTD_ENV=production`
- `ACTD_SESSION_SECRET` long random value
- `ACTD_HOST=127.0.0.1`
- `ACTD_PORT=8765`
- `ACTD_DATA_DIR` a directory on disk that survives restarts
- `ACTD_COOKIE_SECURE=1`
- `ACTD_CANONICAL_ORIGIN=https://aicrashtestdummies.com`
- `ACTD_REDIRECT_HOSTS=-` until step 4 below, then `app.aicrashtestdummies.com,www.aicrashtestdummies.com`
- `ACTD_OPERATOR_TOKEN` for `/admin/visitors` and operator routes. An email address does not grant this.
- `ACTD_ENGINE_INVOCATION_ENABLED=1`
- `ACTD_ENGINE_BASE_URL=http://127.0.0.1:5001`
- `ACTD_MAX_RUN_BUDGET_USD` a positive number. It is not sent to Team Talk.
- `AICTD_BOUNDARY_TOKEN` the boundary bearer token
- Stripe stays unset until a price exists: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` or `STRIPE_PAYMENT_LINK`, `STRIPE_CREDITS_PER_PURCHASE`

Webhook URL, when Stripe is turned on: `https://aicrashtestdummies.com/api/billing/stripe-webhook`

cloudflared ingress, on the tunnel that already fronts the shell. Use that tunnel's existing CNAME target. Do not invent a new tunnel id.

```yaml
ingress:
  - hostname: aicrashtestdummies.com
    service: http://127.0.0.1:8765
  - hostname: www.aicrashtestdummies.com
    service: http://127.0.0.1:8765
  - hostname: app.aicrashtestdummies.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Order:

1. Install this shell with `ACTD_REDIRECT_HOSTS=-` and restart it. `https://app.aicrashtestdummies.com` keeps working.
2. Add the ingress hostnames above and reload cloudflared.
3. Change the apex DNS record from the Pages CNAME (`aicrashtestdummies.pages.dev`) to the tunnel CNAME, proxied. Point `www` at the same tunnel CNAME if `www` should 301. `app` already uses the tunnel.
4. Set `ACTD_REDIRECT_HOSTS=app.aicrashtestdummies.com,www.aicrashtestdummies.com` and restart the shell. `app` and `www` then 301 to the same path on the apex.
5. In Stripe, register the webhook on the apex URL above. Do not register it on `app.`

Existing sessions on `app.` do not follow the redirect. People sign in again on the apex.

## What is stored

Pull request 1 (`cursor/account-quota-billing-79af`, commit `2feb52a`) stores this in SQLite and nothing else. There is no retention job. Rows remain until the database file is deleted, except where a row says otherwise.

- Accounts: email, scrypt password hash, created time, role, email-verified flag. No account-deletion route.
- Sessions: SHA-256 of the cookie, CSRF token, created time, expiry (14 days). The raw cookie is not stored. A session row is deleted on logout, on revoke-all, or when that expired cookie is presented. Expired rows that are never presented again are not swept.
- Submissions: `runs.question` is the question text, at most 4000 characters on that commit. Kept with the run.
- Reports: `runs.result_json` is the JSON the shell recorded from the engine, including untrusted model text when the engine sent it. Also status, detail, engine code, and engine reference. Kept with the run.
- Credits: ledger accounts, reservations, and ledger entries. A `reserved` hold that never left the shell is released after `ACTD_RESERVATION_TTL_SECONDS` (default 900). The run row is not deleted. A hold that was sent and then lost is not auto-released.
- Payments: event id, user id, credit count, and SHA-256 of the webhook body. The raw webhook body is not stored.
- Request counts: day, whether a session was present, user id if so, and a path class. Not the URL and not an IP.
- Rate limits: an HMAC of the session secret and the client address, plus a window and a count. The raw IP is not stored. The HMAC does not rotate daily and is not deleted on a schedule.
- Uploads: none. Multipart requests are rejected. No IP column. No visitor table.

This branch adds, on top of that:

- The stored submission is the text actually sent (question, paste, and text extracted from a file), cut to 12000 characters. The file and the zip are not written to disk.
- `visit_hits`: day, time, a daily HMAC of the client address, a human/bot/unknown label, path, and referrer host plus path. Raw IPs are not stored. Rows are not expired.
- `operator_sessions`: SHA-256 of an operator cookie, 12 hours.

The shell sends that same truncated text to `127.0.0.1:5001` as `document_text`. This repository does not decide how long Team Talk keeps it.

## Deploy

Do not deploy this branch from this VM. The apex stays on Pages until the cutover above. Pages cannot run the Python shell.

## Rollback

1. Stop the Python process.
2. Point the apex CNAME back at the previous Pages target.
3. Keep the SQLite file. Do not delete it if any account has signed up.
4. Set `ACTD_REDIRECT_HOSTS=-` if `app.` must serve the shell again.

## Dell checkout

`/home/chris/aicrashtestdummies` has local work that is not in git and must survive:

- modified `index.html`
- modified `start.html`
- untracked `assets/bg-hero-mobile.jpg`
- untracked `assets/bg-hero.jpg`
- untracked `assets/bg-lower.jpg`
- untracked `css/lab.css`

This branch changes `index.html`, `start.html`, and `css/site.css`. It does not contain the four untracked files. Do not reset, clean, or force-push the Dell checkout.

The account shell is a separate directory, `/home/chris/aictd-account-shell`. Copy this branch there. Do not reset the website checkout at `/home/chris/aicrashtestdummies`.

If that checkout is merged later, preserve the local files first:

```bash
mkdir -p "$HOME/actd-local-preserve"
cp -a index.html start.html "$HOME/actd-local-preserve/"
cp -a assets/bg-hero-mobile.jpg assets/bg-hero.jpg assets/bg-lower.jpg css/lab.css "$HOME/actd-local-preserve/"
git fetch origin cursor/pages-safe-live-links-79af
git merge --no-ff origin/cursor/pages-safe-live-links-79af
```

If `index.html` or `start.html` conflicts, keep the local visual edits and re-apply the `/start` link. Copy the four preserved files back if a command removed them. Do not run `git clean`, `git reset --hard`, `git checkout -- index.html start.html`, or `git push --force`.

`css/lab.css` is separate from `css/site.css`. This branch does not overwrite `lab.css`.

## Not implemented

- Email verification or password reset. No mail provider is configured, and this process does not send mail.
- A published price. Checkout stays off until Stripe is configured, and the pages do not invent one.
- Engine retention. The shell does not control how long Team Talk keeps the text it was sent.
- A live card charge from this repository. No Stripe secret is committed.
