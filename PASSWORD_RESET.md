# Password reset

The flow is implemented end to end — backend, website and app. This file covers
the one part that is configuration rather than code, and the security rules
that are easy to break by accident later.

## Is it working?

```bash
cd backend
python check_email.py                  # report the configuration
python check_email.py you@example.com  # and send a real test message
```

`GET /api/health` also answers `"email": "configured"` or `"not configured"`,
which is worth putting in a deployment check.

---

## 1. Turn on email delivery

**This is almost certainly why "forgot password does not work."** The code is
fine; there is nothing to send with. `MAIL_USERNAME` and `MAIL_PASSWORD` ship
empty in `.env`, so `mail_is_configured()` is false and the reset link is
written to the server log instead of the inbox.

The failure is invisible from the outside, and that is deliberate: the API
answers *"If an account exists for that email, a reset link is on its way"*
whether or not the address is registered, because a different answer would let
anyone test which emails have accounts. That same generic reply also hides a
mail outage. Hence `check_email.py`.

### The sending account

`support@creepycode.com`, a **Google Workspace** mailbox — `creepycode.com`'s
MX record points at `smtp.google.com`. Everything in `.env` is set except the
password.

This is a better position than a personal `@gmail.com` address:

- **SPF is already published** — `v=spf1 include:_spf.google.com ~all` — so
  mail from this account is authenticated rather than suspect.
- **The sender can be branded.** `MAIL_DEFAULT_SENDER="F2H <support@creepycode.com>"`
  works because you own the domain. Gmail rewrites a sender you do not own.
- **It delivers to anyone.** `smtp.gmail.com` with an authenticated Workspace
  account reaches recipients outside your organisation — the "Gmail and
  Workspace users only" limit people quote applies to Google's
  *unauthenticated* `aspmx` relay, which is a different option entirely
  ([Google's documentation](https://support.google.com/a/answer/176600?hl=en)).
- Roughly **2,000 messages a day**, far past what a launch needs.

### Generating the App Password

**The mailbox password will not work.** Google stopped accepting account
passwords over SMTP for Workspace on 1 May 2025. It fails with *"Username and
Password not accepted"* and nothing in the error explains why.

1. **2-Step Verification must be on** for `support@creepycode.com`. App
   Passwords do not appear as an option until it is.
2. Signed in **as that account**, go to
   <https://myaccount.google.com/apppasswords>.
3. Name it `F2H backend` and create it.
4. Copy the **16 characters**. Google shows them once. The spaces are cosmetic.

Then in `backend/.env`, replace the one placeholder:

```
MAIL_PASSWORD=abcdefghijklmnop
```

Restart, then `python check_email.py you@example.com`.

**If the App Passwords page is missing entirely,** a Workspace admin has turned
them off: Admin console → Security → Authentication. An admin can re-enable
them for the organisation, or you switch to OAuth, which is a code change.

### Also worth doing

**Check DKIM.** SPF is published; DKIM is separate and is not on by default in
Workspace. Admin console → Apps → Google Workspace → Gmail → Authenticate
email → generate the key and add the TXT record. Without DKIM, forwarded mail
fails authentication and reset emails land in spam — and a reset the user never
sees is the same as no reset at all.

**Consider a no-reply alias.** Reset mail sent from `support@` invites replies
to a mailbox someone has to read. `noreply@creepycode.com` as a Workspace alias
costs nothing and keeps support triage clean.

### If you outgrow it

Amazon SES, Resend, Postmark and Brevo all speak SMTP, so moving is four lines
in `.env` and no code change.

### `FRONTEND_URL` matters

The email carries two links: a web one built from `FRONTEND_URL`, and an
`f2h://reset-password?token=…` deep link for the app. `FRONTEND_URL` defaults
to `http://localhost:5173`, which is *your* machine — a phone opening it gets
nothing. The deep link is there precisely so the email still works from a
phone, but set `FRONTEND_URL` to the real site before launch.

---

## 2. How the security works

Worth reading before changing any of it.

**Tokens are stored hashed.** 32 random bytes, kept as a SHA-256 digest. The
raw token exists only in the email. A stolen database cannot be used to reset
anyone's account — which is the entire reason for the hash, so do not "simplify"
it to storing the token.

**One live token per account.** Requesting a new link invalidates the previous
one, and completing a reset burns every outstanding token for that user.

**Sixty-minute expiry**, in `TOKEN_TTL_MINUTES`.

**No account enumeration.** `/auth/forgot-password` answers identically for a
registered and an unregistered address. Do not add a friendly "no account with
that email" — it converts the endpoint into a list of your users.

**Rate limited.** 5 per hour and 20 per day per IP on the request endpoint, 10
per hour on the reset itself.

**A reset signs every other session out.** This one was added later and is the
part most likely to be undone by accident.

Tokens here are stateless: a 24-hour access token and a 30-day refresh token,
with nothing server-side to revoke. So in the situation this feature exists for
— *somebody else is in my account* — resetting the password used to change
nothing for the intruder. They kept working access for the rest of the day and
could refresh it for a month.

Now `users.password_changed_at` is stamped on every reset and every password
change, and the JWT loader refuses any token whose `iat` predates it. The
refresh route checks the same thing explicitly, because the 30-day token is the
one that matters most.

Two details that look like they could be tidied up but cannot:

- **The microsecond truncation** (`changed_at.replace(microsecond=0)`).
  `password_changed_at` has microseconds; a JWT's `iat` is whole seconds. Drop
  the truncation and the token handed to the person who *just* reset their
  password is refused about half the time — non-deterministically, depending on
  where in the second the reset landed. `tests/test_session_invalidation.py`
  checks the boundary at five different microsecond offsets for this reason.
- **NULL means no cutoff.** The migration adds the column empty rather than
  backfilling it to `NOW()`. Backfilling would sign out every user on the
  platform the moment it ran.

**Device tokens are cleared too.** A locked account can still be leaking: the
intruder's phone keeps receiving push notifications about the owner's orders.
`_invalidate_sessions` deletes the account's FCM registrations, and the owner's
own device re-registers on its next launch.

---

## 3. Where the code is

| File | Role |
| --- | --- |
| `app/routes/auth.py` | `/forgot-password`, `/reset-password/verify`, `/reset-password`, `/refresh` |
| `app/services/auth_service.py` | Token issue and redemption, `_invalidate_sessions` |
| `app/models/password_reset.py` | The token table, hashing, TTL |
| `app/services/mail_service.py` | Delivery, and the log fallback |
| `app/__init__.py` | The JWT loader that refuses stale tokens |
| `mobile/lib/features/auth/password_screens.dart` | Both app screens |
| `mobile/lib/core/services/deep_link_service.dart` | `f2h://reset-password` |
| `frontend/src/pages/ForgotPasswordPage.jsx` | The web pages |

Run `mysql -u root -p f2h_db < database/password_changed_at.sql` on an existing
database.
