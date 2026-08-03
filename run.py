#!/usr/bin/env python3
"""
F2H Backend Entry Point
Run: python run.py
"""
import os

import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import socketio

app = create_app()

if __name__ == '__main__':
    # The Werkzeug debugger hands an interactive Python console to anyone who
    # can reach it, so debug never switches on by accident and the server binds
    # to localhost unless a host is chosen deliberately.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    host = os.environ.get('FLASK_RUN_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_RUN_PORT', 5000))

    if debug and host not in ('127.0.0.1', 'localhost'):
        raise SystemExit(
            'Refusing to start: debug mode must not be exposed on a non-local '
            f'interface ({host}). Unset FLASK_DEBUG or bind to 127.0.0.1.'
        )

    socketio.run(app, host=host, port=port, debug=debug)
