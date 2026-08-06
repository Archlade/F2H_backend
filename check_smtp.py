#!/usr/bin/env python3
"""Test the mail credentials with nothing but the Python standard library.

    python3 check_smtp.py                    # log in only, send nothing
    python3 check_smtp.py you@example.com    # log in and send a test message

Deliberately has no dependencies — no Flask, no python-dotenv, no virtualenv.
`check_email.py` boots the whole application, which is the better test once the
backend runs on this machine, but it is useless for answering "is this password
right" from a laptop where the venv belongs to another operating system.

Nothing here is imported by the app. It is a diagnostic, safe to delete.
"""

import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

ENV = Path(__file__).with_name('.env')


def read_env():
    """Parse .env well enough for this job: KEY=value, quotes stripped."""
    if not ENV.exists():
        sys.exit(f'No .env found at {ENV}')
    values = {}
    for line in ENV.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main():
    env = read_env()
    host = env.get('MAIL_SERVER', '')
    port = int(env.get('MAIL_PORT') or 587)
    user = env.get('MAIL_USERNAME', '')
    # Google displays app passwords in groups of four; the spaces are cosmetic
    # and must not be sent.
    password = env.get('MAIL_PASSWORD', '').replace(' ', '')
    sender = env.get('MAIL_DEFAULT_SENDER') or user

    print(f'\n  server    {host}:{port}')
    print(f'  account   {user}')
    print(f'  password  {len(password)} characters')
    print(f'  sender    {sender}\n')

    if not (host and user and password):
        sys.exit('  MAIL_SERVER, MAIL_USERNAME and MAIL_PASSWORD must all be set.')
    if password.startswith('REPLACE_'):
        sys.exit('  MAIL_PASSWORD is still the placeholder.')
    if len(password) != 16:
        print(f'  Note: a Google App Password is exactly 16 characters, this is {len(password)}.')
        print('  If this is the mailbox password it will be refused — Google stopped')
        print('  accepting those over SMTP on 1 May 2025.\n')

    try:
        print('  connecting…')
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            print('  TLS negotiated')
            smtp.login(user, password)
            print('  AUTH OK — Google accepted the credentials\n')

            if len(sys.argv) < 2:
                print('  Pass an address to send a real test message:')
                print('    python3 check_smtp.py you@example.com\n')
                return 0

            to = sys.argv[1]
            msg = EmailMessage()
            msg['Subject'] = 'F2H — email delivery test'
            msg['From'] = sender
            msg['To'] = to
            msg.set_content(
                'This is a test from check_smtp.py.\n\n'
                'If you are reading this, password reset emails will reach your users.\n'
            )
            smtp.send_message(msg)
            print(f'  Sent to {to}.')
            print('  Check the inbox, and the spam folder — a first message from a new\n'
                  '  sending address often lands there.\n')
            return 0

    except smtplib.SMTPAuthenticationError as error:
        detail = error.smtp_error.decode('utf-8', 'replace')
        print(f'\n  REFUSED: {error.smtp_code} {detail}\n')
        print('  Almost always one of these:')
        print('   1. This is the mailbox password, not a 16-character App Password.')
        print('   2. The App Password was made on a different Google account.')
        print('      It has to be created while signed in as ' + user + '.')
        print('   3. 2-Step Verification was turned off again, which voids app passwords.\n')
        return 1
    except (OSError, smtplib.SMTPException) as error:
        print(f'\n  Could not reach {host}:{port} — {type(error).__name__}: {error}\n')
        print('  Some networks and ISPs block outbound port 587. Try a phone hotspot.\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
