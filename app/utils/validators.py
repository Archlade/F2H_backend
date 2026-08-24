"""Shared input validation helpers."""

import re

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


def normalise_phone(phone):
    """Reduce a typed Indian number to its bare 10 digits, or None.

    People write the same number half a dozen ways — `9876543210`,
    `+91 98765 43210`, `098765 43210`, `91-9876543210`. All of them are one
    number, and storing them differently means a farmer and a customer with the
    same contact look like two.

    The country code and trunk prefix are stripped rather than rejected: a form
    that refuses `+91…` is a form people retype in irritation.
    """
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]          # +91 98765 43210
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]          # 098765 43210
    return digits or None


def phone_problem(phone):
    """Return a human-readable problem with the phone number, or None.

    Ten digits. That is the whole rule.

    Punctuation, spaces, a `+91` country code and a leading `0` are all stripped
    before counting, so `9876543210`, `+91 98765 43210` and `098765 43210` are
    one number and all three pass — a form that refuses two of the three is a
    form people retype in irritation.

    What is deliberately *not* checked is the first digit. Requiring 6–9 meant
    requiring an Indian mobile, which refused landlines and anyone signing up
    from outside India. The length check catches what it was really there for —
    a typo or a half-typed number — without deciding what kind of phone somebody
    is allowed to own.
    """
    if not (phone or '').strip():
        return 'Phone number is required'

    digits = normalise_phone(phone)
    if not digits:
        return 'Phone number is required'
    if len(digits) != 10:
        return 'Enter a 10-digit phone number'
    return None


# Deliberately not RFC 5322. The full grammar accepts quoted strings, comments
# and bare IP literals — none of which anyone types into a signup form, and all
# of which make the pattern unreadable. This accepts what real addresses look
# like and rejects the rest.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Typos that silently send a password-reset into the void. The cost of getting
# one wrong is a signup refused for a real address, so this stays short and only
# lists domains where the misspelling has no legitimate twin.
_EMAIL_TYPOS = {
    'gmial.com': 'gmail.com', 'gmai.com': 'gmail.com', 'gmail.co': 'gmail.com',
    'gnail.com': 'gmail.com', 'gmail.con': 'gmail.com', 'yahooo.com': 'yahoo.com',
    'yaho.com': 'yahoo.com', 'hotmial.com': 'hotmail.com', 'outlok.com': 'outlook.com',
    'rediffmial.com': 'rediffmail.com',
}


def email_problem(email):
    """Return a human-readable problem with the email address, or None.

    Format only. Whether the mailbox exists cannot be known without sending to
    it, and this app has no verification step — so the honest goal is catching
    the address that was mistyped, not proving the address is real.

    Case is not normalised here; the caller stores whatever it was given. That
    is deliberate — the local part of an address is case-sensitive per the spec,
    even though every provider anyone uses treats it otherwise.
    """
    value = (email or '').strip()
    if not value:
        return 'Email is required'
    if len(value) > 254:
        # The maximum length of a forward path in SMTP.
        return 'That email address is too long'
    if ' ' in value:
        return 'An email address cannot contain spaces'
    if value.count('@') != 1:
        return 'Enter a valid email address, like name@example.com'
    if not _EMAIL_RE.match(value):
        return 'Enter a valid email address, like name@example.com'

    domain = value.rsplit('@', 1)[1].lower()
    if domain in _EMAIL_TYPOS:
        return f'Did you mean {_EMAIL_TYPOS[domain]}? Please check the address'
    if domain.endswith('.'):
        return 'Enter a valid email address, like name@example.com'
    return None


# India Post PIN codes. The first digit is the postal region and runs 1–8;
# 0 and 9 are not allocated, which makes a leading zero the single most common
# way a mistyped PIN slips through a length check.
_PIN_RE = re.compile(r'^[1-8]\d{5}$')

# The states and union territories India Post delivers to. Kept as a set rather
# than free text so "Kerela" and "Tamilnadu" are caught at signup instead of at
# a doorstep.
INDIAN_STATES = {
    'andaman and nicobar islands', 'andhra pradesh', 'arunachal pradesh', 'assam',
    'bihar', 'chandigarh', 'chhattisgarh', 'dadra and nagar haveli and daman and diu',
    'delhi', 'goa', 'gujarat', 'haryana', 'himachal pradesh', 'jammu and kashmir',
    'jharkhand', 'karnataka', 'kerala', 'ladakh', 'lakshadweep', 'madhya pradesh',
    'maharashtra', 'manipur', 'meghalaya', 'mizoram', 'nagaland', 'odisha',
    'puducherry', 'punjab', 'rajasthan', 'sikkim', 'tamil nadu', 'telangana',
    'tripura', 'uttar pradesh', 'uttarakhand', 'west bengal',
}

# Common spellings and old names that mean a real state.
_STATE_ALIASES = {
    'orissa': 'odisha', 'pondicherry': 'puducherry', 'nct of delhi': 'delhi',
    'new delhi': 'delhi', 'tamilnadu': 'tamil nadu', 'kerela': 'kerala',
    'karnatka': 'karnataka', 'maharastra': 'maharashtra', 'j&k': 'jammu and kashmir',
    'uttaranchal': 'uttarakhand', 'bangalore': 'karnataka',
}


def normalise_state(state):
    """The canonical state name, or None if it is not one we deliver to."""
    value = re.sub(r'\s+', ' ', (state or '').strip().lower())
    value = _STATE_ALIASES.get(value, value)
    return value if value in INDIAN_STATES else None


def postal_code_problem(postal_code):
    """Return a human-readable problem with the PIN code, or None."""
    digits = ''.join(ch for ch in (postal_code or '') if ch.isdigit())
    if not digits:
        return 'PIN code is required'
    if len(digits) != 6:
        return 'A PIN code is 6 digits'
    if not _PIN_RE.match(digits):
        return 'That PIN code does not exist — Indian PIN codes start 1 to 8'
    return None


def state_problem(state):
    """Return a human-readable problem with the state, or None."""
    if not (state or '').strip():
        return 'State is required'
    if normalise_state(state) is None:
        return f'"{state.strip()}" is not a state we recognise'
    return None


# First digit of the PIN -> the states that digit covers. India Post allocates
# the leading digit by region, so a Kerala address with a PIN starting 1 is a
# transposition or a copy-paste from somewhere else.
_PIN_REGIONS = {
    '1': {'delhi', 'haryana', 'himachal pradesh', 'jammu and kashmir', 'ladakh', 'punjab', 'chandigarh'},
    '2': {'uttar pradesh', 'uttarakhand'},
    '3': {'rajasthan', 'gujarat', 'dadra and nagar haveli and daman and diu'},
    '4': {'chhattisgarh', 'goa', 'madhya pradesh', 'maharashtra'},
    '5': {'andhra pradesh', 'karnataka', 'telangana'},
    '6': {'kerala', 'lakshadweep', 'puducherry', 'tamil nadu'},
    '7': {'andaman and nicobar islands', 'arunachal pradesh', 'assam', 'manipur',
          'meghalaya', 'mizoram', 'nagaland', 'odisha', 'sikkim', 'tripura', 'west bengal'},
    '8': {'bihar', 'jharkhand'},
}


def address_problem(state, postal_code):
    """Check the state and PIN separately, then check they agree.

    The cross-check is the one that earns its place: each field can be
    individually valid and still describe nowhere. Under cash on delivery a
    wrong address is a wasted trip with produce in the van, so it is worth
    refusing at the point of entry rather than discovering at the door.

    Deliberately a warning-free pass when the region is unknown — India Post
    reassigns ranges, and refusing a real address is worse than accepting an
    odd one.
    """
    problem = state_problem(state)
    if problem:
        return problem
    problem = postal_code_problem(postal_code)
    if problem:
        return problem

    canonical = normalise_state(state)
    digits = ''.join(ch for ch in postal_code if ch.isdigit())
    region = _PIN_REGIONS.get(digits[0])
    if region and canonical not in region:
        return (f'PIN {digits} is not in {state.strip()} — '
                'please check the PIN code and state match')
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
