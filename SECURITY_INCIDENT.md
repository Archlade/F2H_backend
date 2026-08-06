# Exposed credentials — what happened and what to do

**5 August 2026.** `backend/.env` was committed to
`github.com/alvinjoseph582963-stack/f2h_backend`, which is a **public**
repository. It has been there since the first commit. Anyone could read it
without signing in to GitHub.

Automated scanners crawl public repositories for `.env` files continuously,
usually finding them within minutes of a push. **Treat every value below as
already taken**, not as at risk.

## What was exposed

| Value | Why it matters |
| --- | --- |
| `JWT_SECRET_KEY` | The worst one. Anyone holding it can forge a valid admin token — no password needed, no login attempt in your logs, full control of the platform. |
| `SECRET_KEY` | Flask session signing. |
| `DB_PASSWORD` | Your MySQL password. |
| `ADMIN_PASSWORD` | The seeded admin account. |
| `MAIL_PASSWORD` | Set to the same string. |
| `ADMIN_EMAIL` | Which account the above unlocks. |

**One password was used for `ADMIN_PASSWORD`, `MAIL_PASSWORD` and
`DB_PASSWORD`.** The same string was also in `.env.example`, which is committed
by design. If that password is used on any personal account — email, banking,
anything — it is burned and needs changing there too. Reuse is what turns one
leaked file into several compromised accounts.

---

## Done for you

- [x] `SECRET_KEY` and `JWT_SECRET_KEY` regenerated (48 random bytes each).
      **Every existing session is now invalid**, which is the point — it also
      evicts anyone holding a forged token.
- [x] `MAIL_USERNAME`, `MAIL_PASSWORD`, `ADMIN_PASSWORD` replaced with
      placeholders so no stale secret lingers on disk.
- [x] The real password removed from `.env.example`, with a warning header.
- [x] `.env` untracked (`git rm --cached`), file kept on disk.
- [x] `.venv` untracked as well — 5,025 files that never belonged in git.
      The index went from 5,123 tracked files to 97.

---

## Still yours to do

### 1. Make the repo private — now

GitHub → the repo → **Settings → General → Danger Zone → Change visibility →
Private**. One click. It does not remove the history, but it stops the bleeding
while you do the rest.

### 2. Rotate what only you can rotate

| What | Where |
| --- | --- |
| The Google account password, if `9255jom1JO!` is used there | <https://myaccount.google.com/security> |
| MySQL password | `ALTER USER 'root'@'localhost' IDENTIFIED BY '<new>';` then update `DB_PASSWORD` in `.env` |
| Admin account password | Set `ADMIN_PASSWORD` in `.env` and re-run `python seed_admin.py`, or change it in the app |

Use a different password for each. A manager is worth the ten minutes.

### 3. Replace the repository

The repo has **2 commits, 0 forks, 0 stars, 0 issues**, so deleting and
recreating is cleaner and faster than rewriting history — and it leaves no
orphaned blobs that GitHub can still serve by SHA.

```bash
# 1. On GitHub: Settings → Danger Zone → Delete this repository
# 2. Create a new one, PRIVATE this time, same name
# 3. Locally:
cd backend
rm -rf .git
git init
git add .                      # .gitignore now excludes .env and .venv
git status                     # CHECK: .env must not be listed
git commit -m "F2H backend"
git remote add origin https://github.com/<you>/f2h_backend.git
git push -u origin main
```

That `git status` line is not optional. Read it before you commit.

### 4. Check the other repository

`github.com/Archlade/F2Hmarket` does **not** track `backend/.env` — I checked.
Confirm it is private anyway, and that nothing else in it carries a real
credential.

---

## So it cannot happen again

`.gitignore` already listed `.env`. That is not enough: **`.gitignore` only
applies to files git is not already tracking.** Once a file is committed, the
ignore rule is silently irrelevant, which is exactly how this survived.

A pre-commit hook is the check that actually holds. Save as
`backend/.git/hooks/pre-commit` and `chmod +x` it:

```bash
#!/bin/sh
# Refuse to commit a .env or anything that looks like a live secret.
if git diff --cached --name-only | grep -qE '(^|/)\.env$'; then
  echo "BLOCKED: .env is staged. It must never be committed."
  exit 1
fi
```

Also worth turning on: GitHub **Settings → Code security → Secret scanning
and push protection**, which is free on all repositories and blocks a push
containing a recognised credential.

---

## Was anything actually done with it?

Unknown, and probably unknowable. Things worth checking:

- **Database.** Unexpected rows, missing rows, unfamiliar admin accounts:
  `SELECT id, email, created_at FROM users WHERE role_id = 3;`
- **Google account.** <https://myaccount.google.com/device-activity> for
  sign-ins you do not recognise.
- **The repo's traffic graph** (Insights → Traffic) shows clones and views for
  the last 14 days. Clones by anyone other than you are worth noting.

Rotating everything is the fix regardless of what you find. Do that first.
