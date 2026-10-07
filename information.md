# information.md — HR Review App

Comprehensive technical reference: what the project is, how data flows
through it, and its design at both high and low level, including the
reasoning behind the key decisions. For install steps and configuration,
see `README.md`.

---

## 1. What this is

A web app for running employee performance-review cycles:
1. An HR admin opens a review cycle.
2. Employees each nominate 3–5 colleagues to review them, and their manager
   approves, rejects or adds reviewers.
3. The approved colleagues write and submit ratings and comments.
4. The direct manager sees all of that feedback merged automatically and
   anonymously, then writes and releases a final review.
5. The employee reads only that final review plus combined scores, never an
   individual peer review.

HR gets an org-wide completion dashboard, and managers see progress across
their whole reporting line.

It's a rebuild of an earlier Tkinter/sqlite3 desktop prototype into a
proper server-rendered web app with roles, review cycles as a real concept,
an audit trail, migrations, and tests.

---

## 2. Tech stack

| Layer | Choice | Why (short) |
|---|---|---|
| Web framework | **FastAPI** | async-capable, Pydantic-adjacent validation, free `/docs` |
| Interactivity | **htmx 2** (vendored) + View Transitions API | SPA-like navigation and toasts with no build step; progressive enhancement |
| Theming | CSS design tokens, dark + light | one token set, two value sets; toggle persisted in localStorage |
| Templates | **Jinja2**, server-rendered | no frontend build step, one `uvicorn` command runs everything |
| ORM / DB | **SQLAlchemy 2.0** over **SQLite** (WAL, FKs enforced) | one `database.py`, no raw `sqlite3` calls in the app; Postgres via one env var |
| Migrations | **Alembic**, run automatically at startup | schema can evolve under live data; pre-Alembic DBs are adopted in place |
| Sessions | Signed cookie via **itsdangerous**, with a per-user `session_version` | no external API clients, so an opaque server-verified cookie beats a JWT; version bump = global sign-out |
| CSRF | Double-submit token, app-wide dependency | every POST checked; no route can forget |
| Password hashing | **Argon2id** (`argon2-cffi`); legacy PBKDF2-HMAC-SHA256 still verifies and is lazily upgraded | memory-hard vs GPU/ASIC cracking; no forced reset — see §4.4 |
| Rate limiting | DB-backed `login_attempts` table, per username + per IP | shared across worker processes, survives restarts |
| Tests | **pytest** + FastAPI `TestClient` | 273 tests: data layer, auth, permission matrix, workflows, review approval & anonymity, hardening, migrations, config |
| CI | **GitHub Actions** | installs deps, runs pytest on push/PR |

Full dependency list: `requirements.txt`.

---

## 3. High-Level Design (HLD)

### 3.1 System shape

A server-rendered monolith. Browser talks HTTP to FastAPI; FastAPI renders
Jinja2 HTML directly; FastAPI talks to the database through SQLAlchemy. No
API layer, no separate frontend, no background workers, no external
services. Every piece of shared state (sessions are verified against it,
rate-limit counters live in it) is in the database, so running several
uvicorn workers is safe.

```
 Browser  <---HTTP (forms + cookies)--->  FastAPI app(s)  <---SQLAlchemy--->  SQLite / Postgres
                                              |
                                        Jinja2 templates
```

### 3.2 Major subsystems

- **Config** (`app/config.py`) — every environment variable in one place;
  secret-key loading (required in production, generated + persisted in dev).
- **Auth & sessions** (`app/security.py`, `app/deps.py`, `routers/auth.py`,
  `routers/account.py`) — hashing/verification, session tokens, login
  throttling, reset tokens, and the `require_login` / `require_role(...)`
  dependency gates every other router builds on.
- **CSRF** (`app/csrf.py`) — middleware that issues the token cookie and an
  app-wide dependency that verifies it on unsafe methods.
- **Data layer** (`app/database.py`, `app/models.py`) — engine/session
  factory, SQLite pragmas, and the eight ORM models.
- **Migrations** (`alembic.ini`, `migrations/`, `app/db_migrations.py`) —
  Alembic revisions and the startup runner.
- **Domain logic** (`app/services.py`, `app/language_filter.py`, `app/org.py`,
  `app/feedback.py`) — profanity filtering, rating computation,
  username/password rules;
  reporting-tree traversal and loop detection; who approves reviewers, and
  the anonymous merge of peer reviews for the manager. Pure functions over
  the session, no HTTP.
- **Audit trail** (`app/audit.py`) — `log_action()` stages an audit row in
  the caller's transaction.
- **Pagination** (`app/pagination.py`) — `Page` + `paginate()` /
  `paginate_list()`, rendered by `templates/_pagination.html`.
- **Routers** (`app/routers/*.py`) — `auth`, `account`, `dashboard`,
  `cycles`, `assignments`, `reviews`, `users`, `team` (approvals and final
  reviews).
- **Templates** (`app/templates/*.html`, `app/template_utils.py`) — a
  context processor injects `nav_user`, `nav_position`, `csrf_token` and
  any pending toast (`flashes`) into every render. `_ui.html` holds the
  ring and stepper macros.
- **Frontend behaviour** (`app/static/app.js`, `app/static/vendor/htmx.min.js`,
  `app/flash.py`, `app/progress.py`, `app/audit_view.py`):
  - **Theme toggle:** the choice is applied before first paint by an
    inline head script, so there's no flash of the wrong theme.
  - **htmx boost:** pages swap with view transitions, and htmx is
    configured to also swap 4xx pages.
  - **Toasts:** a signed flash cookie carries the message across the
    redirect, and the page that shows it clears it.
  - **Progress:** ring and stepper data are computed in `progress.py`.
  - **Audit timeline:** `audit_view.py` turns audit rows into sentences,
    with lookups batched per page.
- **Seed data** (`app/seed.py`) — a demo org of 8 named people (HR Admin,
  Director, Manager, 5 employees) on first run
  (development only by default).

### 3.3 Roles and what each can do

*Positions vs roles:* there are three access **roles** (below). The
**position** shown in the UI is derived from role plus the org chart by
`org.position_title()`: HR Admin, **Director** (a manager who manages other
managers), Manager, or Employee. Names display as "Priya Sharma"
(`User.display_name`, from the `first.last` username).

| Role | Can do |
|---|---|
| `employee` | Nominate 3–5 reviewers (once per active cycle). Write/submit reviews for **approved** assignments while the cycle is active. Read their manager's released final review and combined scores (`/my/review`) — **never individual peer reviews**. Change their own password. |
| `manager` | Everything an employee can, **plus**: approve/reject/add reviewers for **direct** reports; see direct reports' peer feedback **merged and anonymised** and write and release their final review; progress-only view of the rest of their reporting line (skip-level). |
| `hr_admin` | Create/activate/close review cycles. Assign reviewers. Approve nominations for anyone. For people with **no manager**, HR is the approver and also **writes and releases their final review**. View the org-wide completion dashboard. View raw peer reviews read-only (`/reviews/view`, dispute resolution). Retract a submitted review in the active cycle (audit-logged with full content), unless a final review was already released from it. Provision users, change roles and reporting lines, issue password-reset links. |

Enforced by `app/deps.py::require_role(*roles)` as a FastAPI dependency on
every route; the full role×route matrix is tested in
`tests/test_permissions.py`.

### 3.4 Core lifecycle (state machine, informally)

```
Cycle:        draft --activate--> active --close--> closed   (closed can be re-activated)
              (activating one closes whichever was active; DB allows at most one active)
Assignment:   nominated by reviewee -> pending --manager/HR--> approved | rejected
              added by manager or HR -> approved immediately          (active cycle only)
Review:       none --submit (approved, cycle active)--> locked --HR retract (cycle active)--> unlocked
FinalReview:  (author: direct manager, or HR if the employee has no manager)
              none --save--> draft --release (>= 3 submitted reviews)--> released (locked,
              numbers frozen; blocks further peer submissions, retracts and reviewer changes)
```

A `Review` row doesn't exist until its reviewer's first submit; a retract
clears its fields and unlocks it rather than deleting the row, and the
audit row keeps everything that was cleared.

---

## 4. Low-Level Design (LLD)

### 4.1 Directory layout

```
HR-PERFORMANCE-REVIEW-PLATFORM/
  app/
    main.py            app instance, lifespan (migrate + seed), middleware, routers, exception handlers
    config.py          env settings, secret-key loading
    database.py        engine, SQLite pragmas (WAL + foreign_keys), SessionLocal, Base, get_db()
    db_migrations.py   upgrade_database(): Alembic at startup, adopts pre-Alembic DBs
    models.py          User, ReviewCycle, ReviewerAssignment, Review, AuditLog, LoginAttempt, PasswordResetToken, FinalReview
    security.py        hashing (dual-algorithm, lazy rehash), session tokens, login throttling, reset tokens
    csrf.py            token cookie middleware + verify_csrf dependency
    deps.py            resolve_session_user, get_current_user, require_login, require_role, get_page
    audit.py           log_action() -> stages an AuditLog row (caller commits)
    services.py        overall rating, rating/username/password validation (password strength rules)
    language_filter.py find_banned_language(): swear words + forms, disguised spellings, slurs, insults, slang
    org.py             reporting_tree(), would_create_cycle(), manager_problem()
    feedback.py        approval rules, merge_peer_reviews(), release_final_review(), thresholds
    pagination.py      Page, paginate(), paginate_list()
    seed.py            DEMO_ACCOUNTS + seed_if_empty() -> 8-person demo org + one active cycle
    template_utils.py  shared Jinja2Templates + context processor (nav_user, csrf_token)
    routers/
      auth.py          /login, /logout (POST), /register
      account.py       /account/password, /reset-password/{token}
      dashboard.py     /, /my/dashboard, /my/review, /manager/reports, /admin/dashboard
      cycles.py        /admin/cycles, /admin/cycles/{id}/activate, /admin/cycles/{id}/close
      assignments.py   /assignments/select, /admin/assignments
      reviews.py       /reviews/pick, /reviews/write/{id}, /reviews/{id}/retract, /reviews/view/{username} (HR only)
      users.py         /admin/users, /admin/users/{id}, /admin/users/{id}/reset-link
      team.py          /team/approvals[/{id}/approve|reject], /team/reviewers/add, /team/reviews/{username}
      audit.py         /admin/audit?category=&page=  (HR-only timeline)
    templates/*.html   one per page, extending base.html; _pagination.html partial; _ui.html (ring + stepper macros)
    static/style.css   design tokens (dark + light), components, view transitions, toasts, rings, stepper, timeline
    static/app.js      theme toggle, toast timers, ring animation (progressive enhancement)
    static/vendor/htmx.min.js   htmx 2.0.4 (0BSD), vendored -- no CDN at runtime
    static/favicon.svg
    flash.py           signed one-time toast cookie + middleware that clears it
    progress.py        Ring / Step data: cycle_overview() for HR, my_progress() for employees
    audit_view.py      audit rows -> readable timeline sentences
  migrations/
    env.py, script.py.mako
    versions/0001_initial_schema.py   the original create_all() schema (baseline)
    versions/0002_auth_hardening.py   session_version, one-active-cycle index, login_attempts, password_reset_tokens
    versions/0003_review_approval_and_final_reviews.py   assignment status/source/decided_at, final_reviews
    versions/0004_account_invites.py   password_reset_tokens.purpose (invite | reset)
  alembic.ini
  tests/
    conftest.py          per-test SQLite DB, CSRF-aware TestClient, login/logout/nominate_and_approve helpers
    fixture_org.py       seed_test_org(): the fixed test org (hr_admin, director1, manager1, user1-user4)
    test_demo_seed.py    the demo org, its passwords, and derived positions
    test_database.py     model constraints
    test_auth.py         hashing, sessions, rate limiter
    test_permissions.py  role x route matrix
    test_routes.py       end-to-end workflows
    test_feedback.py     nomination/approval, anonymity, merged view, final-review release & locking
    test_language_and_passwords.py   profanity filter (catches + false positives), password rules
    test_hardening.py    CSRF, validation, closed cycles, audit, password reset, hierarchy, pagination
    test_migrations.py   model/migration parity, legacy adoption, downgrade
    test_config.py       secret-key rules
  scripts/
    create_admin.py              first hr_admin for a production DB
    migrate_password_prefix.py   one-time: re-tag legacy raw PBKDF2 blobs as "pbkdf2$..."
  .github/workflows/ci.yml
  requirements.txt, README.md, information.md (this file)
```

### 4.2 Data model (schema)

**`users`**
| column | type | notes |
|---|---|---|
| id | PK int | |
| username | str(64), unique index | `[A-Za-z0-9_.-]{3,64}` enforced at registration |
| password_hash | str | `"argon2id$<encoded>"` (current) or `"pbkdf2$<base64 salt+hash>"` (legacy, still verifiable) |
| role | str | `employee` \| `manager` \| `hr_admin` |
| manager_id | FK -> users.id, nullable | self-referencing; any depth; must point at a `manager`; loops rejected |
| session_version | int, default 0 | embedded in session cookies; bumped on password change/reset |
| created_at | datetime | |

**`review_cycles`**
| column | type | notes |
|---|---|---|
| id | PK int | |
| name | str(128) | e.g. "Q1 2026" |
| start_date / end_date | datetime | end must be after start |
| status | str | `draft` \| `active` \| `closed` |
| *(partial unique index)* | | `uq_one_active_cycle` on `status WHERE status = 'active'` — the database itself refuses a second active cycle |

**`reviewer_assignments`** — "who reviews whom, in which cycle"
| column | type | notes |
|---|---|---|
| id | PK int | |
| cycle_id | FK -> review_cycles.id | |
| reviewee_id / reviewer_id | FK -> users.id | neither may be an hr_admin; never the same person |
| status | str, default `approved` | `pending` \| `approved` \| `rejected`; only `approved` can be written |
| source | str, default `nominated` | `nominated` (by the reviewee) \| `manager` \| `hr` |
| decided_at | datetime, nullable | when the manager/HR decided (who decided is in the audit log) |
| *(unique constraint)* | | `(cycle_id, reviewee_id, reviewer_id)` |

**`reviews`** — one row per assignment, created lazily on first submit
| column | type | notes |
|---|---|---|
| id | PK int | |
| assignment_id | FK, **unique** | 1:1 with assignment |
| `{area}_text`, `{area}_rating` | text, int(1-5) | six skill areas: work_quality, productivity, communication, collaboration, initiative, punctuality |
| additional_notes | text | |
| overall_rating | float | mean of the ratings, computed at submit time |
| is_locked | bool | `True` on submit |
| submitted_at | datetime, nullable | |

**`final_reviews`** — the manager's consolidated review, one per employee per cycle
| column | type | notes |
|---|---|---|
| id | PK int | |
| cycle_id, employee_id, manager_id | FKs | unique `(cycle_id, employee_id)`; `manager_id` = the author (current direct manager) |
| summary, strengths, improvements | text | shown to the employee once released |
| final_rating | int 1-5, nullable | required to release; pre-suggested from the rounded peer average |
| status | str | `draft` (invisible to the employee) \| `released` (locked) |
| stats_snapshot | text (JSON), nullable | written on release: `{responses, overall_average, skills: {area: avg}}` — numbers only, never comments |
| created_at, updated_at, released_at | datetime | |

**`audit_log`**
| column | type | notes |
|---|---|---|
| actor_id | FK -> users.id, nullable | null for pre-auth events (failed login) |
| action | str | e.g. `login_success`, `login_failure`, `password_rehashed`, `change_password`, `password_reset`, `issue_password_reset`, `issue_invite`, `accept_invite`, `submit_review`, `retract_review`, `create_cycle`, `activate_cycle`, `close_cycle`, `nominate_reviewers`, `approve_reviewer`, `reject_reviewer`, `add_reviewer`, `assign_reviewer`, `save_final_review`, `release_final_review`, `create_user`, `update_user`, `logout` |
| target | str | e.g. `assignment:12`, `cycle:3`, `user:alice` |
| details | text (JSON) | action-specific; `retract_review` holds the full cleared review |
| timestamp | datetime | |

**`login_attempts`** — one row per failed login
| column | type | notes |
|---|---|---|
| key | str(128) | `user:<name>` or `ip:<addr>`; composite index with attempted_at |
| attempted_at | datetime | rows older than the window are pruned on each new failure |

**`password_reset_tokens`**
| column | type | notes |
|---|---|---|
| user_id | FK -> users.id | |
| token_hash | str(64), unique | SHA-256 hex of the raw token; the raw token is never stored |
| issued_by_id | FK -> users.id, nullable | the HR admin |
| expires_at | datetime | created + 1 hour |
| used_at | datetime, nullable | set on use, or when a newer token for the same user is issued |
| purpose | str, default `reset` | `invite` (a new account's first password, valid 3 days) or `reset` (valid 1 hour) |

ER sketch:

```
User --1:N--> ReviewerAssignment (as reviewee / as reviewer)
User --1:N--> User (manager_id self-FK, any depth)
User --1:N--> PasswordResetToken
ReviewCycle --1:N--> ReviewerAssignment
ReviewerAssignment --1:1--> Review
User --1:N--> FinalReview (as employee / as author manager); ReviewCycle --1:N--> FinalReview
User --1:N--> AuditLog (as actor)
```

### 4.3 Request lifecycle (concrete example: submitting a review)

1. Browser `GET /reviews/pick` — the CSRF middleware makes sure a
   `csrf_token` cookie exists; `require_role(employee, manager)` resolves
   the session cookie to a `User` (signature, expiry, and `session_version`
   all checked against the DB).
2. Handler lists this reviewer's **approved** assignments in the active
   cycle that have no locked review.
3. `GET /reviews/write/{id}` — `_get_owned_assignment()` 404s if missing,
   403s if it isn't this reviewer's or isn't approved yet. If the cycle is closed the page says so
   instead of showing the form.
4. `POST /reviews/write/{id}`:
   a. app-wide `verify_csrf` compares the form's `csrf_token` to the cookie
      (403 on mismatch) before the handler runs;
   b. re-checks ownership, approval, lock state, that the cycle is still
      active, and that the manager hasn't already released the final review;
   c. parses and validates ratings (1–5);
   d. `language_filter.find_banned_language()` across all free text —
      one regex pass covering swear words with their prefixes/suffixes
      ("fucker", "bullshit"), disguised spellings ("f*ck", "sh1t"), slurs,
      insults and slang, matched on whole words so "assess" and
      "Scunthorpe" pass; returns every offending word at once;
   e. on failure, re-renders with the error and the typed values kept;
   f. on success: creates/updates the `Review`, computes `overall_rating`,
      locks it, stages the `submit_review` audit row, and commits **once** —
      review and audit row are atomic.

### 4.4 Auth & session mechanics

- **Login** (`POST /login`): `login_lockout_seconds()` checks two
  independent limits in `login_attempts` — 5 failures per username and 20
  per client IP, each over a 5-minute window. Unknown usernames still run
  one Argon2id verify against a dummy hash (`burn_password_check`), so
  response time doesn't reveal whether an account exists. A failure
  records rows for both keys (pruning expired rows as it goes); a success
  clears only that username's counter, never the IP's. On success the
  session cookie is issued and the CSRF token is rotated.
- **Password verification is algorithm-aware.** `verify_password` branches
  on the stored prefix: `argon2id$` → `PasswordHasher.verify`; `pbkdf2$` →
  PBKDF2-HMAC-SHA256 with `hmac.compare_digest`. A malformed stored hash
  fails closed (returns `False`) rather than raising.
- **Lazy rehash.** A successful `pbkdf2$` verify re-hashes the just-typed
  plaintext with Argon2id, overwrites the stored hash, and writes a
  `password_rehashed` audit row. No bulk migration, no forced reset.
  `scripts/migrate_password_prefix.py` is the offline companion that
  re-tags pre-prefix raw PBKDF2 blobs as `pbkdf2$...`.
- **Sessions.** `itsdangerous.URLSafeTimedSerializer` over
  `{uid, v}` (user id + `session_version`), 8-hour max age, `HttpOnly`,
  `SameSite=Lax`, `Secure` in production. `deps.resolve_session_user`
  re-fetches the user on every request and rejects the cookie if the
  version doesn't match — so a password change or reset signs that user
  out on every device immediately. (Changing your own password re-issues
  the current browser's cookie so you stay signed in there.)
- **Secret key.** `config.load_secret_key()`: from `HR_REVIEW_SECRET_KEY`;
  in production it's mandatory (≥32 chars) and startup fails without it;
  in development a random key is generated once into `.secret_key`
  (created with `O_EXCL`, so concurrent workers agree on one key).
- **CSRF.** `csrf_cookie_middleware` gives every visitor a random
  `csrf_token` cookie and exposes it to templates; every form renders it as
  a hidden field; `verify_csrf` (registered as an app-level dependency, so
  it covers every route) rejects POST/PUT/PATCH/DELETE whose field doesn't
  match the cookie. Logout is POST-only for the same reason.
- **Password rules** (`services.password_problem`): at least 8 characters with an uppercase letter, a lowercase letter, a number and a special character, and not containing the username; max 256
  characters. One error message lists everything missing. Checked on
  registration, password change, reset and `scripts/create_admin.py`, i.e.
  whenever a password is set; existing passwords keep working. The rules
  text is shown under every new-password field.
- **New accounts get an invite, not a password.** HR creates the account
  with no password: the stored value is an unusable `unset$…` marker, and
  logins to it are refused at the cost of a full Argon2 check, so timing
  doesn't reveal pending accounts. HR is shown a one-time **invite link**,
  valid 3 days and stored only as a SHA-256 digest. The new person opens it
  and chooses their own password, so HR never knows it. The audit log records
  `create_user` and `issue_invite`, then `accept_invite`. If the link is lost,
  HR can create a new invite, which voids the old one.
- **Password change & reset.** `/account/password` requires the current
  password. HR's "Create reset link" (`/admin/users/{id}/reset-link`)
  stores only a SHA-256 of a 256-bit random token, voids earlier unused
  tokens for that user, and shows the link once (`Cache-Control:
  no-store`). `/reset-password/{token}` accepts it once, within an hour;
  the reset also clears that username's login lockout. `Referrer-Policy:
  same-origin` keeps the token in the URL from leaking to other origins.
- **Errors.** `401` → redirect to `/login`; `403` → `forbidden.html` (with
  the specific reason, e.g. CSRF); `404` → `not_found.html`; missing/invalid
  form fields → a 400 HTML page instead of FastAPI's JSON 422.

### 4.5 Templates / rendering

All routers share one `templates` object from `app/template_utils.py`,
with absolute template/static paths so the app runs from any working
directory. The context processor injects `nav_user` (resolved with the same
session rules as the routes) and `csrf_token` into every render. It opens
its DB session through `request.app.dependency_overrides` (falling back to
the real `get_db`) so it reads the same database as the route — including
the per-test DB in the test suite. List pages include
`_pagination.html`, driven by a `Page` object (`?page=N`, 20 per page,
junk or out-of-range values clamp instead of erroring).

### 4.6 Testing approach

`tests/conftest.py` builds a fresh on-disk SQLite file per test (via
pytest's `tmp_path`), seeded with a small fixed test org
(`tests/fixture_org.py`; deliberately separate from the demo data so
changing demo accounts never breaks a test), and
overrides `get_db` to point every route at it — real ORM queries and
constraints (including foreign keys and the partial unique index), no
mocks. It sets a fixed test secret key and redirects the app's startup
engine to a throwaway file, so the suite never touches a developer's real
database or key file. The client subclass `CSRFAwareClient` fills in the
CSRF field on every POST the way a real form would; tests that exercise
CSRF rejection pass an explicit bad token. `test_migrations.py` runs the
real Alembic revisions and uses `alembic.autogenerate.compare_metadata` to
assert the migrated schema equals the models.

---

## 5. Request flow diagrams (textual)

**Employee happy path, one cycle:**
```
login -> GET /my/dashboard
      -> GET /assignments/select -> POST (nominate 3-5 eligible colleagues; all pending)
      -> ... manager approves / rejects / adds ...
      -> ... someone's approved nomination of me appears ...
      -> GET /reviews/pick -> GET /reviews/write/{id} -> POST (submit, locks)
      -> ... manager releases my final review ...
      -> GET /my/review  (manager's final review + combined scores; never individual reviews)
```

**HR admin cycle setup:**
```
login -> GET /admin/cycles -> POST (create draft cycle)
      -> POST /admin/cycles/{id}/activate   (closes the previously active cycle)
      -> GET /admin/assignments -> POST     (assign specific reviewer pairs, optional)
      -> GET /admin/dashboard               (completion totals + paginated table)
      -> POST /reviews/{id}/retract         (only if a resubmission is needed; logged with content)
      -> POST /admin/cycles/{id}/close
```

**HR admin user management:**
```
GET /admin/users -> GET /admin/users/{id}
   -> POST (change role / reporting line; loops and orphaned reports rejected)
   -> POST /admin/users/{id}/reset-link  -> hand the one-time link to the user
user: GET /reset-password/{token} -> POST (new password) -> /login?notice=reset
```

**Manager path:**
```
login -> GET /manager/reports   (status per person across the whole reporting line, direct + skip-level)
      -> GET /team/approvals    (approve / reject nominations, add reviewers, for direct reports)
      -> GET /team/reviews/{report}  (merged anonymous feedback: per-skill averages, shuffled comments)
      -> POST action=save (draft) ... POST action=release (needs >= 3 submitted; locks, freezes numbers)
```

---

## 6. Known limitations

See README.md's "Known limitations / what I'd do next": no email delivery
for reset links, per-IP throttling trusts the direct peer address (needs
proxy-header config behind a load balancer), SQLite's single-writer
ceiling, no matrix/dotted-line reporting, an anonymity threshold of 3 (a floor, not a guarantee), and no
self-assessment, goals or calibration.

**SQLite write concurrency.** `app/database.py` enables WAL mode on every
SQLite connection. By default SQLite locks the whole file for the duration
of a write; with WAL, readers don't block behind a writer (and vice versa)
because writes go to a separate log that's periodically checkpointed. It
doesn't allow concurrent *writers* — two simultaneous writes still queue —
so it raises the ceiling without removing it. The data layer is plain
SQLAlchemy and the migrations are dialect-neutral (the partial index is
declared for both SQLite and Postgres), so moving to Postgres is a
`HR_REVIEW_DATABASE_URL` change.
