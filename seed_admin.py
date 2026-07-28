#!/usr/bin/env python3
"""
Seed script to create the initial admin account.
Run AFTER database schema is applied:
    python seed_admin.py

Requires .env with:
    ADMIN_EMAIL=...
    ADMIN_PASSWORD=...
"""
import os
import sys

import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import db
from app.models import User, Role

app = create_app()


def seed_admin():
    admin_email = os.environ.get('ADMIN_EMAIL')
    admin_password = os.environ.get('ADMIN_PASSWORD')

    if not admin_email or not admin_password:
        print('ERROR: ADMIN_EMAIL and ADMIN_PASSWORD must be set in .env')
        sys.exit(1)

    with app.app_context():
        # Ensure roles exist
        roles = ['customer', 'farmer', 'admin']
        for role_name in roles:
            if not Role.query.filter_by(name=role_name).first():
                db.session.add(Role(name=role_name))
        db.session.commit()

        admin_role = Role.query.filter_by(name='admin').first()
        existing = User.query.filter_by(email=admin_email.lower()).first()
        if existing:
            print(f'Admin already exists: {admin_email}')
            return

        import bcrypt
        pw_hash = bcrypt.hashpw(admin_password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')

        admin = User(
            role_id=admin_role.id,
            email=admin_email.lower().strip(),
            password_hash=pw_hash,
            first_name='Admin',
            last_name='F2H',
            is_active=True,
            is_verified=True,
        )
        db.session.add(admin)
        db.session.commit()
        print(f'✓ Admin account created: {admin_email}')


if __name__ == '__main__':
    seed_admin()
