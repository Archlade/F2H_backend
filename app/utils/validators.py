"""Shared input validation helpers."""

# A short list of the passwords credential-stuffing tools try first.
COMMON_PASSWORDS = {
    'password', 'password1', 'password123', '12345678', '123456789', '1234567890',
    'qwerty123', 'letmein1', 'welcome1', 'admin123', 'iloveyou', 'sunshine',
    'football', 'baseball', 'trustno1', 'passw0rd', 'abc12345', 'changeme',
}

MIN_PASSWORD_LENGTH = 10


def password_problem(password):
    """Return a human-readable problem with the password, or None if it's fine."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f'Password must be at least {MIN_PASSWORD_LENGTH} characters'
    if len(password) > 128:
        return 'Password must be 128 characters or fewer'
    if password.lower() in COMMON_PASSWORDS:
        return 'That password is too common — please choose another'
    if password.isdigit() or password.isalpha():
        return 'Password must mix letters with numbers or symbols'
    return None


def phone_problem(phone):
    """Return a human-readable problem with the phone number, or None.

    Deliberately loose: it strips the punctuation people type (spaces, dashes,
    brackets, a leading +) and only insists on a plausible run of digits. The
    aim is to catch typos and empty submissions, not to police formats — a
    farmer whose number does not fit a strict Indian pattern still needs to be
    reachable when an order is on its way.
    """
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if not digits:
        return 'Phone number is required'
    if len(digits) < 7:
        return 'That phone number looks too short'
    if len(digits) > 15:
        # E.164 caps at 15 digits including the country code.
        return 'That phone number looks too long'
    return None


def clamp_page(page, per_page, max_per_page=50):
    """Keep pagination inputs inside sane bounds.

    Without this, page=0 produces a negative SQL offset and page=-1 or a huge
    per_page turns a list endpoint into a cheap denial-of-service lever.
    """
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page or 20)
    except (TypeError, ValueError):
        per_page = 20
    return max(1, page), max(1, min(per_page, max_per_page))
