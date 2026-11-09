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

## Deploy

Do not deploy this branch from this VM. The apex already points at Pages, and the Pages project itself could not be identified. There is no existing Worker route to attach this Python process to. Pages cannot run it. Publishing the repo root as a static site would expose `engine/identity-map.json`. The Python server returns 404 for `/engine/*`.

A later host, chosen on purpose, needs:

1. This branch, reviewed
2. The environment variables above
3. The SQLite directory on a disk that survives restarts
4. HTTPS in front of `python -m app`
5. A separate decision about the apex, `www`, and the wildcard. Do not repoint them as part of merging this branch.

No migration of old accounts is required. The database starts empty. There is no historic traffic table to import.

## Rollback

1. Stop the Python process.
2. Point the site back at the previous static publish.
3. Keep the SQLite file. Do not delete it if any account has signed up.
4. To roll the code back later, restore a copy of that SQLite file with the same schema. This branch's schema is created with `CREATE TABLE IF NOT EXISTS` and does not drop tables.

## Dell checkout

`/home/chris/aicrashtestdummies` has local work that is not in git and must survive:

- modified `index.html`
- modified `start.html`
- untracked `assets/bg-hero-mobile.jpg`
- untracked `assets/bg-hero.jpg`
- untracked `assets/bg-lower.jpg`
- untracked `css/lab.css`

This branch changes `index.html`, `start.html`, and `css/site.css`. It does not contain the four untracked files. Do not reset, clean, or force-push the Dell checkout.

Safe merge, on the Dell machine, after this branch is reviewed:

```bash
mkdir -p "$HOME/actd-local-preserve"
cp -a index.html start.html "$HOME/actd-local-preserve/"
cp -a assets/bg-hero-mobile.jpg assets/bg-hero.jpg assets/bg-lower.jpg css/lab.css "$HOME/actd-local-preserve/"
git fetch origin cursor/account-quota-billing-79af
git merge --no-ff origin/cursor/account-quota-billing-79af
```

If `index.html` or `start.html` conflicts, keep the local visual edits and re-apply only the account links (`/account/login` and `/app`) if the local page still says sign-in is closed. Copy the four preserved files back if a command removed them. Do not run `git clean`, `git reset --hard`, `git checkout -- index.html start.html`, or `git push --force`.

`css/lab.css` is separate from `css/site.css`. This branch does not overwrite `lab.css`.

## Not implemented

- Email verification or password reset. No mail provider is configured, and this process does not send mail.
- A real Team Talk run, repository ingest, or crash report. The status page says so.
- A live card charge. Live mode also stays closed until `ACTD_MODEL_COST_POLICY` points at a real rate note.
- Cloudflare analytics. Analytics Read was denied. No traffic total is stored here.
- A visitor count. Historic requests are not users, and none were imported.
