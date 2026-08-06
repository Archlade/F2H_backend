"""Outbound email.

SMTP is optional. When MAIL_SERVER / MAIL_USERNAME aren't configured the
message is written to the application log instead of being dropped, so the
password-reset flow is fully testable on a dev machine with no mail account.
Configure the MAIL_* variables in .env to switch to real delivery — no code
changes required.
"""

from flask import current_app

try:
    from flask_mail import Message
except ImportError:  # pragma: no cover - Flask-Mail is optional at runtime
    Message = None

from ..extensions import mail


def mail_config_problem():
    """The specific reason mail cannot be sent, or None when it can.

    Six things have to line up and "not configured" is useless on its own —
    the credentials can be perfectly correct while the package that sends
    them is missing, which looks identical in the log and is nothing like the
    same problem. Naming the failure is the difference between a one-line fix
    and an afternoon.
    """
    if Message is None or mail is None:
        return ('Flask-Mail is not installed in the Python environment running '
                'this server. Install it with:  pip install Flask-Mail==0.10.0  '
                '(check you are in the right virtualenv — a venv built on '
                'another machine or OS will not work here)')

    missing = [name for name in
               ('MAIL_SERVER', 'MAIL_USERNAME', 'MAIL_PASSWORD', 'MAIL_DEFAULT_SENDER')
               if not current_app.config.get(name)]
    if missing:
        return (f'{", ".join(missing)} not set. Check backend/.env, and that the '
                'server was restarted after editing it')

    if str(current_app.config.get('MAIL_PASSWORD', '')).startswith('REPLACE_'):
        return 'MAIL_PASSWORD is still the placeholder from .env'

    return None


def mail_is_configured() -> bool:
    """True only when there is a complete set of credentials to send with.

    A half-filled config must not be treated as working, or every reset
    request would stall on an SMTP connection that is going to be refused.
    """
    return mail_config_problem() is None


def send_email(to: str, subject: str, body: str, html: str = None) -> bool:
    """Returns True if the message was handed to an SMTP server, False if it
    was only logged. Never raises — a mail outage must not break the request."""
    problem = mail_config_problem()
    if problem:
        current_app.logger.warning(
            '\n─── EMAIL NOT SENT ───\n'
            'Reason: %s\n\n'
            'To: %s\nSubject: %s\n\n%s\n'
            '──────────────────────',
            problem, to, subject, body,
        )
        return False

    try:
        msg = Message(subject=subject, recipients=[to], body=body, html=html)
        mail.send(msg)
        return True
    except Exception:
        current_app.logger.exception('Failed to send email to %s', to)
        return False


def send_password_reset_email(user, reset_url: str, app_url: str = None) -> bool:
    """`app_url` is the f2h:// deep link. It is offered alongside the web link
    because someone reading this on their phone usually cannot reach the web
    address — in development that is a localhost URL on someone else's machine.
    """
    subject = 'Reset your F2H password'
    app_line = f"On your phone, open this instead:\n\n{app_url}\n\n" if app_url else ''
    body = (
        f"Hi {user.first_name},\n\n"
        "We received a request to reset the password on your F2H account.\n"
        "Open the link below to choose a new one:\n\n"
        f"{reset_url}\n\n"
        f"{app_line}"
        "This link expires in 1 hour and can only be used once.\n"
        "If you didn't ask for this, you can ignore this email — your password "
        "will stay the same.\n\n"
        "— The F2H team"
    )
    app_html = f"""\
  <p style="font-size:13px;color:#6b7280;margin:0 0 20px">
    Reading this on your phone? <a href="{app_url}" style="color:#16a34a;font-weight:600">
    Open it in the F2H app</a> instead.
  </p>""" if app_url else ''

    html = f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:520px;margin:0 auto;color:#1f2937">
  <h2 style="color:#166534;margin-bottom:8px">Reset your password</h2>
  <p>Hi {user.first_name},</p>
  <p>We received a request to reset the password on your F2H account.
     Choose a new one using the button below.</p>
  <p style="margin:28px 0">
    <a href="{reset_url}"
       style="background:#16a34a;color:#fff;padding:12px 24px;border-radius:8px;
              text-decoration:none;font-weight:600;display:inline-block">Reset password</a>
  </p>
  {app_html}
  <p style="font-size:13px;color:#6b7280">
    This link expires in 1 hour and can only be used once.
    If you didn't ask for this, you can ignore this email — your password will stay the same.
  </p>
  <p style="font-size:13px;color:#6b7280;word-break:break-all">
    Button not working? Paste this into your browser:<br>{reset_url}
  </p>
</div>"""
    return send_email(user.email, subject, body, html)
