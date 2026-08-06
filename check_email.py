#!/usr/bin/env python3
"""Prove that password reset emails can actually be delivered.

    python check_email.py                  # just report the configuration
    python check_email.py you@example.com  # and send a real test message

Worth having as its own script because the failure it catches is silent. The
API answers "a reset link is on its way" whether or not SMTP works — that is
deliberate, since a different answer would tell an attacker which addresses
have accounts — so the only way to find out is to look, and the only way most
people find out is a customer who cannot get back into their account.
"""

import sys

from app import create_app
from app.services.mail_service import mail_is_configured, send_email

REQUIRED = ('MAIL_SERVER', 'MAIL_PORT', 'MAIL_USERNAME', 'MAIL_PASSWORD', 'MAIL_DEFAULT_SENDER')


def main():
    app = create_app()
    with app.app_context():
        print('\nMail configuration\n' + '─' * 50)
        for key in REQUIRED:
            value = app.config.get(key)
            if key == 'MAIL_PASSWORD':
                # Length only. An app password in a terminal history or a
                # pasted screenshot is a live credential.
                shown = f'set ({len(str(value))} chars)' if value else '— not set —'
            else:
                shown = value if value else '— not set —'
            mark = ' ' if value else '!'
            print(f' {mark} {key:22s} {shown}')

        ok = mail_is_configured()
        print('─' * 50)
        if not ok:
            print('\nEmail is NOT configured. Reset links are being written to the\n'
                  'server log rather than sent. Fix it in backend/.env:\n'
                  '\n'
                  '  MAIL_USERNAME=you@gmail.com\n'
                  '  MAIL_PASSWORD=<16-character Google App Password>\n'
                  '\n'
                  'A Google App Password is not your account password — see\n'
                  'PASSWORD_RESET.md for how to generate one.\n')
            return 1

        print('\nEmail is configured.\n')

        if len(sys.argv) < 2:
            print('Pass an address to send a real test message:\n'
                  '  python check_email.py you@example.com\n')
            return 0

        to = sys.argv[1]
        print(f'Sending a test message to {to} …')
        delivered = send_email(
            to=to,
            subject='F2H — email delivery test',
            body=('This is a test from check_email.py.\n\n'
                  'If you are reading this, password reset emails will reach '
                  'your users.\n'),
        )
        if delivered:
            print('Handed to the SMTP server. Check the inbox — and the spam folder,\n'
                  'because a brand-new sending address often lands there first.\n')
            return 0

        print('The send failed. The exception is in the application log above.\n'
              'The usual causes, in order:\n'
              '  1. MAIL_PASSWORD is the account password, not an App Password.\n'
              '  2. Two-step verification is off, so App Passwords do not exist yet.\n'
              '  3. The network blocks outbound port 587.\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
