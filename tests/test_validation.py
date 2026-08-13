"""Signup and address validation.

Run with: python -m unittest discover -s tests

These guard the two fields a wrong value costs the most on. An email that never
reaches anyone is an account that cannot be recovered — and with password reset
switched off, cannot be recovered at all. An address that does not exist is,
under cash on delivery, a van-load of produce at the wrong door and nobody to
take the money.

Loaded by path rather than imported as `app.utils.validators`, because that
would pull in `app/__init__.py` and with it Flask. These helpers are deliberately
pure — the only import in the module is `re` — so they can be tested with
nothing installed.
"""

import importlib.util
import pathlib
import unittest

_spec = importlib.util.spec_from_file_location(
    'validators',
    pathlib.Path(__file__).resolve().parent.parent / 'app' / 'utils' / 'validators.py',
)
validators = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validators)

email_problem = validators.email_problem
address_problem = validators.address_problem
postal_code_problem = validators.postal_code_problem
state_problem = validators.state_problem
normalise_state = validators.normalise_state


class EmailMustLookLikeAnAddress(unittest.TestCase):
    """Format only. Whether the mailbox exists cannot be known without sending
    to it, and there is no verification step — so the goal is catching the
    address that was mistyped, not proving it is real."""

    def test_ordinary_addresses_pass(self):
        for value in ('name@example.com', 'a.b+c@sub.domain.co.in',
                      'FIRST.LAST@Example.COM', 'x1@y2.in'):
            with self.subTest(value=value):
                self.assertIsNone(email_problem(value))

    def test_nonsense_is_refused(self):
        # Every one of these was a valid signup before: the route only checked
        # that the field was non-empty.
        for value in ('asdf', 'no@domain', 'two@@at.com', '@nolocal.com',
                      'trailing@dot.com.', 'nodomain@'):
            with self.subTest(value=value):
                self.assertIsNotNone(email_problem(value))

    def test_spaces_get_their_own_message(self):
        # The commonest paste error, and worth naming rather than answering
        # with a generic "invalid".
        self.assertIn('space', email_problem('has space@x.com').lower())

    def test_a_missing_address_says_so(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                self.assertEqual(email_problem(value), 'Email is required')

    def test_common_domain_typos_are_caught(self):
        # gmial.com resolves to nothing, so the signup succeeds and the person
        # never hears from us again.
        self.assertIn('gmail.com', email_problem('x@gmial.com'))

    def test_the_typo_list_does_not_catch_real_domains(self):
        for value in ('x@gmail.com', 'x@mail.com', 'x@gmx.com', 'x@email.com'):
            with self.subTest(value=value):
                self.assertIsNone(email_problem(value))


class ProductionRefusesPlaceholderSecrets(unittest.TestCase):
    """`secret_problem` in app/config.py.

    The original check was "32 characters and not on a blocklist", which sounds
    strict and was not. This project's own key —
    `f2h-dev-secret-key-2024-change-in-production` — is 44 characters and was
    not on the list, so it passed both tests while literally saying it was a
    placeholder. It is also one of the keys published in the repository's
    history, which is what makes it worth a test rather than a comment.
    """

    def setUp(self):
        import re
        src = (pathlib.Path(__file__).resolve().parent.parent
               / 'app' / 'config.py').read_text()
        block = re.search(
            r'INSECURE_DEFAULTS = \{.*?\n\n\ndef secret_problem.*?\n    return None',
            src, re.S).group(0)
        ns = {'re': re}
        exec(block, ns)  # noqa: S102 - reading our own source, no input involved
        self.secret_problem = ns['secret_problem']

    def test_the_key_this_project_actually_shipped_is_refused(self):
        for value in ('f2h-dev-secret-key-2024-change-in-production',
                      'f2h-jwt-secret-key-2024-change-in-production'):
            with self.subTest(value=value):
                self.assertIsNotNone(self.secret_problem('SECRET_KEY', value))

    def test_length_alone_does_not_make_a_secret(self):
        # Every one of these is over 32 characters.
        for value in ('your-secret-key-here-please-replace-it-now',
                      'TODO-set-this-properly-before-launch-okay',
                      'placeholder-value-that-is-quite-long-here',
                      'example-key-do-not-use-in-real-deployment'):
            with self.subTest(value=value):
                self.assertGreater(len(value), 32)
                self.assertIsNotNone(self.secret_problem('SECRET_KEY', value))

    def test_short_keys_are_refused(self):
        for value in ('', 'changeme', 'a' * 31):
            with self.subTest(value=value):
                self.assertIn('characters', self.secret_problem('SECRET_KEY', value))

    def test_real_generated_keys_are_accepted(self):
        """The check must not be so eager that a legitimate token trips it.

        This ran at 20,000 rather than a token or two on purpose. The first
        version matched markers as bare substrings, and a random 64-character
        token contains a stray "todo" or "example" about once in 14,000 — so a
        small sample passed, and production would eventually have refused to
        boot on a perfectly good key with an incomprehensible message. Markers
        are now matched as whole words; the volume here is what would catch a
        regression back to substrings.
        """
        import secrets
        for _ in range(20_000):
            token = secrets.token_urlsafe(48)
            self.assertIsNone(self.secret_problem('SECRET_KEY', token),
                              f'refused a real token: {token}')

    def test_a_marker_inside_a_word_is_not_a_placeholder(self):
        # 'todo' buried in random characters is a coincidence, not an intention.
        # These must pass, or the check is back to substring matching.
        for value in ('xKtodoQ7' + 'a' * 40, 'zzexamplezz' + 'b' * 35):
            with self.subTest(value=value):
                self.assertIsNone(self.secret_problem('SECRET_KEY', value))


class PhoneNumbersAreTenIndianDigits(unittest.TestCase):
    """Ten digits starting 6–9.

    This used to accept any 7-to-15 digit run, on the reasoning that an unusual
    number still has to be reachable. Under cash on delivery an unreachable
    number is a driver outside a building with produce and no way in, so the
    loose rule cost more than it saved.
    """

    def test_a_plain_mobile_passes(self):
        for value in ('9876543210', '6000000000', '7012345678', '8123456789'):
            with self.subTest(value=value):
                self.assertIsNone(validators.phone_problem(value))

    def test_the_ways_people_actually_type_it_all_pass(self):
        # One number, six spellings. Refusing five of them is a form people
        # retype in irritation.
        for value in ('9876543210', '+91 98765 43210', '098765 43210',
                      '91-9876543210', '98765-43210', '(+91) 9876543210'):
            with self.subTest(value=value):
                self.assertIsNone(validators.phone_problem(value))
                self.assertEqual(validators.normalise_phone(value), '9876543210')

    def test_wrong_length_is_refused(self):
        for value in ('987654321', '98765432101', '12345'):
            with self.subTest(value=value):
                self.assertIn('10-digit', validators.phone_problem(value))

    def test_landlines_and_dropped_digits_are_refused(self):
        # A leading 0-5 means a landline, a short code, or a digit lost off the
        # front — none of which reaches anyone during a delivery.
        for value in ('5876543210', '044 2345 6789', '1234567890'):
            with self.subTest(value=value):
                self.assertIn('6, 7, 8 or 9', validators.phone_problem(value))

    def test_a_missing_number_says_so(self):
        for value in ('', '   ', None, 'abcdefghij'):
            with self.subTest(value=value):
                self.assertEqual(validators.phone_problem(value), 'Phone number is required')


class PinCodesAreSixDigitsStartingOneToEight(unittest.TestCase):

    def test_real_pins_pass(self):
        for pin in ('682001', '110001', '560001', '700001', '800001'):
            with self.subTest(pin=pin):
                self.assertIsNone(postal_code_problem(pin))

    def test_wrong_length_is_refused(self):
        for pin in ('68200', '6820012', '1'):
            with self.subTest(pin=pin):
                self.assertIn('6 digits', postal_code_problem(pin))

    def test_leading_zero_or_nine_is_refused(self):
        # India Post never allocated regions 0 or 9, which makes a leading zero
        # the single most common way a mistyped PIN passes a length check.
        for pin in ('012345', '912345'):
            with self.subTest(pin=pin):
                self.assertIsNotNone(postal_code_problem(pin))


class StatesAreRealPlaces(unittest.TestCase):

    def test_canonical_names_pass(self):
        for state in ('Kerala', 'Tamil Nadu', 'West Bengal', 'Delhi', 'Ladakh'):
            with self.subTest(state=state):
                self.assertIsNone(state_problem(state))

    def test_common_misspellings_are_accepted_and_corrected(self):
        # Refusing "Kerela" would be technically right and practically hostile.
        self.assertEqual(normalise_state('kerela'), 'kerala')
        self.assertEqual(normalise_state('TAMILNADU'), 'tamil nadu')

    def test_old_names_still_work(self):
        self.assertEqual(normalise_state('Orissa'), 'odisha')
        self.assertEqual(normalise_state('Pondicherry'), 'puducherry')

    def test_invented_places_are_refused(self):
        self.assertIsNotNone(state_problem('Narnia'))
        self.assertIsNone(normalise_state('Narnia'))


class ThePinAndStateMustAgree(unittest.TestCase):
    """The check that earns its place.

    Each field can be individually valid and still describe nowhere — a Kerala
    address with a Delhi PIN passes both separate checks and fails at the door.
    """

    def test_matching_pairs_pass(self):
        for state, pin in (('Kerala', '682001'), ('Tamil Nadu', '600001'),
                           ('Karnataka', '560001'), ('Maharashtra', '400001'),
                           ('Delhi', '110001'), ('West Bengal', '700001'),
                           ('Orissa', '751001')):
            with self.subTest(state=state, pin=pin):
                self.assertIsNone(address_problem(state, pin))

    def test_a_pin_from_the_wrong_region_is_refused(self):
        problem = address_problem('Kerala', '110001')
        self.assertIsNotNone(problem)
        self.assertIn('110001', problem)

    def test_the_field_level_problem_is_reported_first(self):
        # A nonsense state should say so rather than complain about the region,
        # which would be confusing and wrong.
        self.assertIn('Narnia', address_problem('Narnia', '682001'))
        self.assertIn('6 digits', address_problem('Kerala', '68200'))


if __name__ == '__main__':
    unittest.main()
