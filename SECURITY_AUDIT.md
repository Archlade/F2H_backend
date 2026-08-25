# Security and correctness audit — 25 August 2026

Covers the backend, the website and the Flutter app.

**This file lives in the backend repo deliberately.** `Archlade/F2Hmarket` is
public; a document listing where a system is weak does not belong there.
`Archlade/F2H_backend` is private, and `backend/` is a gitlink in the public
repo, so its contents are not exposed through it.

---

## 1. CRITICAL — a public repository still holds your `.env`

**`github.com/alvinjoseph582963-stack/f2h_backend` is public right now**, with
`.env` at the root and a `.venv` directory beside it. `SECURITY_INCIDENT.md`
recorded this on 5 August. Three weeks later the repository is still up and
still public — verified today.

That file contains `JWT_SECRET_KEY`. Anyone holding it can mint a token this
backend will accept as any user, including admin. No password, no login
attempt, nothing in the logs to see. It also carries `SECRET_KEY`,
`DB_PASSWORD`, `ADMIN_PASSWORD`, `MAIL_PASSWORD` and `ADMIN_EMAIL`.

The incident note says `SECRET_KEY` and `JWT_SECRET_KEY` were regenerated
afterwards. Believe that only as far as it can be checked — the Firebase key
was covered by a `.gitignore` rule that did not match, and nobody knew for
three weeks. Rotating costs one restart; proving the exposed values differ from
the live ones costs more than that.

**Do, in this order:**

1. Delete the repository: Settings → scroll to Danger Zone → Delete this
   repository. Not "make private" — a private repo can be made public by
   accident, and the history stays either way.
2. Rotate on the server, then `systemctl restart farmapp`:

   ```bash
   python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
   python3 -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_urlsafe(48))"
   ```

   Every signed-in user is logged out, which is the point — it also evicts
   anyone holding a forged token.
3. Change the MySQL password for the `f2h` user, and `DB_PASSWORD` with it.
4. Change `ADMIN_PASSWORD` and re-run `seed_admin.py`.
5. **If that password was ever used on a personal account, change it there
   too.** The incident note records one string shared across `ADMIN_PASSWORD`,
   `MAIL_PASSWORD` and `DB_PASSWORD`. Reuse is what turns one leaked file into
   several compromised accounts.

---

## 2. HIGH — `.env` and `.env.production` are tracked in the backend repo

Both are in the index of `Archlade/F2H_backend`. That repo is private today, so
nothing is exposed *now* — but this is the arrangement that has already caused
two incidents, and it depends on a setting nobody re-checks.

The Firebase key was the third. It was committed as
`secrets/firebase-service-account.json`, which slipped past the
`*firebase-adminsdk*.json` rule already in `.gitignore` because it had been
renamed. Google revoked it, push died silently, and `/api/health` reported
`configured` throughout. Both of those are now fixed — the key is untracked by
directory, and health verifies the credential against Google.

The same treatment would suit the `.env` files: untrack them, keep
`.env.example` as the committed template, and place the real file per machine.
Say the word and I'll do it — it is the same change as the key, and about five
minutes.

## 3. HIGH — `.venv/` is committed to the backend repo

Thousands of files under `.venv/Lib/site-packages/` — a Windows virtualenv.
`SECURITY_INCIDENT.md` lists this as already removed; it is not. Beyond the
bloat, a committed virtualenv is a supply-chain surface: it pins whatever
happened to be on one machine on one day, forever, including anything later
found vulnerable.

```bash
cd backend
git rm -r --cached .venv
echo ".venv/" >> .gitignore     # already listed, but the rm is what matters
```

---

## 4. MEDIUM

**Rate-limit storage is in-memory.** `Limiter(key_func=get_remote_address,
default_limits=["200 per minute"])` with no `storage_uri`. Counters reset on
every restart and are per-process, so the login limits — `5 per hour` on
sign-in, `10 per 15 minutes` on register — are weaker than they read. With one
gunicorn worker it mostly holds; a second worker halves every limit. Point it
at Redis when convenient.

**`JWT_SECRET_KEY` has a usable default.** `config.py` falls back to
`'dev-jwt-secret-change-in-production'`. If the env var is ever missing in
production the app boots happily and signs tokens with a string that is in the
source. Better to refuse to start:

```python
if os.environ.get('FLASK_ENV') == 'production' and not os.environ.get('JWT_SECRET_KEY'):
    raise RuntimeError('JWT_SECRET_KEY must be set in production')
```

**The public README names the admin account.** `jovelrobin07@gmail.com`, in
`Archlade/F2Hmarket`, with a heading that says it is the admin login. That
halves the work of attacking the account. The rate limits help; not publishing
it helps more.

---

## 5. What is already right

Listed because a good deal of care is visible here, and because knowing which
parts are sound is as useful as knowing which are not.

| Area | Finding |
| --- | --- |
| Password storage | bcrypt, cost 12 |
| Website tokens | httpOnly cookies, not `localStorage` — an XSS cannot read them |
| CSRF | Double-submit, `X-CSRF-TOKEN` from a readable cookie, separate token for refresh |
| App tokens | Keychain / Android Keystore, never SharedPreferences |
| Authorization | `role_required` re-reads the role from the database each request, so a demotion or suspension is immediate despite 24-hour tokens |
| Route coverage | Every write endpoint is authenticated. The 13 open routes are catalogue reads, register, reset-password and the cron endpoints |
| Cron auth | `hmac.compare_digest`, not `==` — not timing-comparable |
| Password reset | Tokens stored hashed, single-use via `used_at`, prior tokens invalidated on issue |
| Rate limiting | Present on all eight auth endpoints, not just login |
| CORS | Locked to `f2hmarket.com` and `www.` — not `*` |
| Cookies | `ProductionConfig` sets `JWT_COOKIE_SECURE = True`, `HTTPONLY`, `SAMESITE=Lax` |
| Cleartext | Blocked on both platforms; exemptions are loopback only, on Android and in iOS ATS |
| SQL | No raw SQL anywhere. Searches use `ilike()` with bound parameters |
| Dangerous builtins | No `eval`, `exec`, `pickle.loads`, `os.system`, `subprocess` |
| Uploads | Extension allowlist, `secure_filename`, `MAX_CONTENT_LENGTH` of 10 MB |
| Frontend bundle | `.env.production` holds only `VITE_API_URL`; no secret is inlined |

---

## 6. Correctness

Clean across all three, as of this session's changes:

- **Backend** — every file under `app/` parses; 21 blueprints registered; no
  undefined helpers.
- **Website** — every relative import resolves; 61 routes declared, all
  components present.
- **App** — no unbalanced files, no broken imports, no unused imports.

One loose end: `shared_preferences` is declared in `pubspec.yaml` and imported
nowhere. Harmless, but it is a dependency being carried for no reason.

---

## 7. Not checkable from here

- **`npm audit` and `pip-audit`.** No outbound network in the environment this
  was run from. Worth running on your machine; dependency CVEs are the one
  category nothing above covers.
- **Whether the live secrets differ from the exposed ones.** Section 1 assumes
  the worst rather than trying to verify, which is the cheaper of the two.
- **The server's real `.env`.** The repo copy of `.env.production` has
  placeholders for `DB_PASSWORD` and `ADMIN_PASSWORD`, so the file in use on the
  server is a different one and its contents were not read.
