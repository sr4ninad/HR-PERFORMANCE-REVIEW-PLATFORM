# HR Review App

A performance-review cycle tool: employees nominate peer reviewers, their
manager approves them, reviewers submit ratings, and the manager writes a
final review from an automatic, anonymous merge of that feedback. HR runs
the cycle. Rebuilt from a Tkinter/sqlite3 prototype into a FastAPI +
SQLAlchemy web app. See `information.md` for the full technical reference
(architecture, data model, request flow, security) and
`tests/test_permissions.py` for the role×route permission matrix.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. On startup the database is migrated to the
latest Alembic revision automatically, and in development an empty database
is seeded with:

| Name | Username | Password | Position |
|---|---|---|---|
| Kavya Iyer | `kavya.iyer` | `Lotus-Harbor-82!` | HR Admin |
| Rajesh Menon | `rajesh.menon` | `Cedar-Summit-19!` | Director |
| Priya Sharma | `priya.sharma` | `Willow-Bridge-47!` | Manager (reports to Rajesh) |
| Arjun Nair | `arjun.nair` | `Copper-Falcon-31!` | Employee (reports to Priya) |
| Sneha Kulkarni | `sneha.kulkarni` | `Amber-River-64!` | Employee (reports to Priya) |
| Rohan Desai | `rohan.desai` | `Granite-Echo-58!` | Employee (reports to Priya) |
| Meera Pillai | `meera.pillai` | `Indigo-Meadow-23!` | Employee (reports to Priya) |
| Vikram Reddy | `vikram.reddy` | `Silver-Monsoon-76!` | Employee (reports to Priya) |

...plus one active cycle, "Q1 2026". Every demo password meets the app's
password rules, and each is different. They're demo credentials only;
production seeds nothing.

Each person's position is worked out from their role and the org chart: a
manager who manages other managers shows as **Director**. Names are shown as
"Priya Sharma", taken from the `first.last` username.

In development a random session-signing
key is generated into `.secret_key` (gitignored) on first run, so there's
still zero setup.

## Configuration

All settings are environment variables, read in `app/config.py`:

| Variable | Default | Notes |
|---|---|---|
| `HR_REVIEW_ENV` | `development` | set to `production` for a real deployment |
| `HR_REVIEW_SECRET_KEY` | *(generated dev key)* | **required** in production, min 32 chars; the app refuses to start without it |
| `HR_REVIEW_DATABASE_URL` | `sqlite:///./hr_review.db` | any SQLAlchemy URL, e.g. Postgres |
| `HR_REVIEW_COOKIE_SECURE` | on in production | HTTPS-only cookies |
| `HR_REVIEW_SEED_DEMO` | on outside production | demo accounts above |

Production bootstrap (no demo accounts are seeded there):

```bash
export HR_REVIEW_ENV=production HR_REVIEW_SECRET_KEY=... HR_REVIEW_DATABASE_URL=...
python scripts/create_admin.py <username>     # prompts for a password
uvicorn app.main:app --workers 4
```

## Schema changes

```bash
# edit app/models.py, then:
alembic revision --autogenerate -m "describe the change"
alembic upgrade head          # or just restart the app
```

`tests/test_migrations.py` fails if the models and migrations ever drift
apart.

## Running tests

```bash
pytest -q
```

Tests run against their own small fixture org (`tests/fixture_org.py`:
`hr_admin`, `director1`, `manager1`, `user1`–`user4`), not the demo data, so
editing the demo accounts never breaks a test.

273 tests:
- `tests/test_database.py`: data-layer constraints.
- `tests/test_auth.py`: hashing (including legacy PBKDF2 and the lazy rehash to Argon2id), sessions, and the database-backed rate limiter.
- `tests/test_permissions.py`: the role×route matrix.
- `tests/test_feedback.py`: reviewer nomination and approval, anonymity guarantees, the merged view, and final-review release and locking.
- `tests/test_demo_seed.py`: the demo org (names, roles, managers), every demo password meets the rules and logs in, and positions are worked out correctly.
- `tests/test_ui.py`: toasts (shown once, forgery-proof, escaped), theme/htmx wiring, rings and steppers, and the audit-log timeline (HR-only, readable, escapes attacker-supplied text, filters keep across pages).
- `tests/test_language_and_passwords.py`: the profanity filter (bad words caught, normal words like "assess" allowed) and the password-strength rules.
- `tests/test_routes.py`: end-to-end workflows.
- `tests/test_hardening.py`: CSRF, input validation, closed-cycle rules, audit atomicity, password change and reset, multi-level reporting, pagination.
- `tests/test_migrations.py`: the schema matches the models, pre-Alembic databases upgrade in place, and downgrades work.
- `tests/test_config.py`: secret-key handling.

## Architecture

- **FastAPI + server-rendered Jinja2 templates**, no separate frontend
  build — `uvicorn app.main:app` is the whole app.
- **Frontend:**
  - **Design tokens and themes:** every color is a token. Dark and light
    themes are the same tokens with different values. A toggle in the nav
    remembers your choice; until you pick, the theme follows the OS.
  - **htmx** (vendored in `app/static/vendor/`, no build step) boosts every
    link and form, so pages swap without full reloads.
    `historyCacheSize: 0` keeps pages, which can hold review content, out
    of `localStorage`. Everything still works with JavaScript off.
  - **View transitions** animate page changes.
  - **Toasts** confirm each action after its redirect, via a short-lived,
    signed cookie.
  - **HR dashboard:** a cycle **stepper**, plus four **progress rings**
    (single-value meters, with text labels and a "Complete" marker, so
    nothing relies on color alone).
  - **Employees** get their own review stepper. It shows no peer-review
    counts, because watching a count tick up would reveal when each
    colleague submitted.
  - **Audit Log page (HR):** the audit table as a readable timeline, with
    category filters.
- **Roles**: `employee`, `manager`, `hr_admin`.
  - **Employees** nominate 3–5 reviewers, write the reviews they're
    approved for, and read their manager's final review.
  - **Managers** approve their direct reports' nominations and write those
    reports' final reviews. For the rest of their reporting line
    (skip-level) they see progress only.
  - **HR** runs cycles, assigns reviewers, manages users, and can read raw
    peer reviews (read-only) to resolve disputes. For people with **no
    manager** (the top of the org), HR approves their reviewers and writes
    their final review, so nobody's feedback is left without an author.
- **Review workflow**: nominate → manager approves, rejects or adds →
  approved reviewers write → the direct manager sees every peer review
  merged automatically (per-skill averages and pooled comments) and writes a
  final review → releasing it locks it and freezes the numbers.
- **Anonymity**: the employee chooses their own nominees, so hiding names
  alone wouldn't be enough. Instead they **never see individual peer
  reviews**. They see only the manager's final review plus combined scores,
  and a review can't be released until at least 3 peer reviews are in. The
  manager's merged view shows no reviewer names or timestamps, and comments
  are shuffled.
- **Review cycles**: reviews are scoped to a `review_cycles` row
  (draft/active/closed). Only one cycle can be active — enforced by a
  partial unique index in the database, not just by application code.
  Reviews can only be written while their cycle is active.
- **Immutability**: submitting a review locks it. The only way to undo one
  is HR's "retract", which unlocks it for rewrite while preserving the full
  original content in the audit log.
- **Audit log**: every state-changing action writes an `audit_log` row *in
  the same transaction* as the change, so there's never an action without
  its audit entry or vice versa.
- **Auth**: Argon2id hashing with lazy migration from legacy PBKDF2; signed
  session cookies carrying a per-user `session_version`, so a password
  change or reset signs the user out everywhere; CSRF tokens on every form;
  database-backed login throttling per username *and* per IP; timing-safe
  login that doesn't reveal which usernames exist.
- **Password rules**: every new password needs at least 8 characters with an uppercase letter, a lowercase letter, a number and a special character, and not containing the username. The rules apply
  whenever a password is set; existing passwords keep working until changed.
- **Language filter**: review comments and final reviews are checked for
  swear words (including forms like "fucker" and disguised spellings like
  "f*ck"), slurs, insults and unprofessional slang, without flagging normal
  words such as "assess" or "Scunthorpe".
- **Invites, not passwords**: HR creates accounts without a password; the new person chooses one via a one-time invite link (3 days), so HR never knows it.
- **Password reset**: users change their own password under *Account*; HR
  can issue a single-use, 1-hour reset link from *Users* (only a SHA-256 of
  the token is stored).
- **Migrations**: Alembic. Startup runs `upgrade head`; a database created
  by the old `create_all()` bootstrap is detected, stamped at the baseline
  revision, and upgraded in place.
- **SQLite in WAL mode with foreign keys enforced**. The data layer is pure
  SQLAlchemy, so Postgres is a `HR_REVIEW_DATABASE_URL` change.

## Known limitations / what I'd do next

- **The language filter is a word list.** It catches common swear words,
  their forms and disguised spellings, but no list is complete; a
  determined person can still find a way around it.

- **No email delivery.** Password-reset links are shown to HR to pass on
  over a trusted channel; there's no SMTP integration or self-service
  "forgot password" by email.
- **Per-IP throttling trusts the direct peer address.** Behind a reverse
  proxy every client shares the proxy's IP; you'd configure trusted
  `X-Forwarded-For` handling (e.g. uvicorn `--proxy-headers`) there.
- **SQLite's single writer.** WAL lets reads proceed during writes, but
  writes still queue; Postgres is the move for real concurrent write load.
- **Matrix reporting.** The org chart is a tree of any depth (one manager
  per person); dotted-line / multiple-manager reporting isn't modeled.
- **Anonymity has a floor, not a guarantee.** With exactly 3 responses,
  someone who knows two of their reviewers' ratings could work out the
  third. Real platforms use the same kind of threshold; a larger one (e.g.
  5) would be stronger. The direct manager also approved the reviewers, so
  they know the pool, as in real systems.
- **No self-assessment or goals.** It covers peer feedback and the
  manager's final review; there are no self-reviews, goals/OKRs or
  calibration.
