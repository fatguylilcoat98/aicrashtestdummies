# AI Crash Test Dummies — migration note

Phase 1 is this static site. Team Talk is not modified by it.

## What was inspected

The product repository was a single reference image, `Aicrashtestdummies.png`. There was no application framework, no auth, and no deploy config.

Team Talk is the live engine at `/opt/team-talk`. It is a FastAPI app on `127.0.0.1:5001` (`team-talk.service`), with JSON and JSONL stores under `memory/` and `sessions/`. There is no customer account system. Uploads live under `uploads/` and are not a public marketing asset path.

Investigation behavior already in that process, and left in place:

- `repo_bundle.py` — zip ingest, path escape checks, symlink skip, secret denylist
- `repo_evidence.py` — normalized paths, sha256 receipts, grep, lexical and Solidity-pattern xref, source conflicts
- `investigation_engine.py` — candidates, blind visibility, Gate Zero, falsifier separation, contradictions, known-issue symbol/path match, morgue, promotion blocks

Public names are not provider names. `engine/identity-map.json` is the seam. The website does not render it.

## How the pieces will connect later

Public site, then an authenticated app shell, then the existing Team Talk engine. Do not write a second investigation core. Do not serve `uploads/` or `sessions/` from this marketing site.

## Feature classes for the later shell

Public primary: start a test, investigation status, report, evidence list.

Public advanced: contradictions, morgue, Gate Zero detail.

Power user: rooms, Wall, journals, Night Shift, Workshop, Director, Reflection.

Admin: provider keys, seat routing, the identity map.

Legacy until replaced: the current Team Talk screens. Nothing in that list was removed.

Not safe to delete without a separate decision: sessions, ledger, receipts, uploaded repositories.

## This phase

Homepage, docs, example report, pricing, and the pre-launch start page. Sign-in and start both land on the pre-launch page. No statistics, testimonials, customers, or prices were invented. The reference image's testimonial and role titles were not copied. Roles are not permanent.
