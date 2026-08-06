"""Paying farmers what they have earned.

Farmers see a balance and ask for it. An admin pays them by UPI or bank
transfer and records the reference.

Unchanged by the move to cash on delivery, and worth saying why: the cash is
collected by F2H at the door, so the platform still holds every rupee until a
farmer redeems it. Only the way money *arrives* changed. Had the farmer kept the
cash instead, this file would invert — the balance would become commission the
farmer owes, and "redeem" would become "settle".

The one rule that must not bend: **a balance can only be spent once.** The
request endpoint re-reads the balance inside the transaction that creates the
payout, and counts money already requested-but-unpaid against it. Two taps on
Redeem must not produce two payouts for the same money.
"""

import logging
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from ..extensions import db
from ..models import LedgerEntry, Payout, User
from ..models.payment import money
from ..services import wallet_service as wallet
from ..utils.locking import lock_row
from ..services.notification_service import create_notification
from ..utils.decorators import role_required
from ..utils.helpers import paginate_response
from ..utils.validators import clamp_page

logger = logging.getLogger(__name__)

payouts_bp = Blueprint('payouts', __name__)

# Payouts that have left the farmer's balance in intent but not yet in fact.
OPEN_STATUSES = ('requested', 'approved')


def _profile(user_id):
    user = User.query.get(user_id)
    return (user.farmer_profile if user else None), user


# ── Farmer: balance and details ───────────────────────────────────────────────

@payouts_bp.route('/wallet', methods=['GET'])
@role_required('farmer', 'admin')
def my_wallet():
    """Balance, lifetime totals, and what is still tied up in undelivered orders."""
    farmer_id = int(get_jwt_identity())
    summary = wallet.summary(farmer_id)

    pending = db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(
        Payout.farmer_id == farmer_id, Payout.status.in_(OPEN_STATUSES)).scalar()

    profile, _ = _profile(farmer_id)
    summary.update({
        'pending_payouts': float(money(pending)),
        # What could be requested right now. Never negative — a refund reversal
        # can put a balance underwater and the farmer should see zero available,
        # not a negative number they cannot act on.
        'available': max(0.0, round(summary['balance'] - float(money(pending)), 2)),
        'minimum_payout': current_app.config.get('MIN_PAYOUT_AMOUNT', 200),
        'payout_details_set': bool(profile and (profile.payout_upi_id or profile.payout_account_number)),
    })
    return jsonify(summary), 200


@payouts_bp.route('/wallet/entries', methods=['GET'])
@role_required('farmer', 'admin')
def my_ledger():
    """Every movement, newest first — the explanation behind the balance."""
    farmer_id = int(get_jwt_identity())
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'))
    query = LedgerEntry.query.filter_by(farmer_id=farmer_id).order_by(LedgerEntry.id.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(paginate_response([e.to_dict() for e in items], total, page, per_page)), 200


@payouts_bp.route('/details', methods=['GET', 'PUT'])
@role_required('farmer')
def payout_details():
    """Where this farmer's money should be sent.

    Behind the farmer's own auth only, and never exposed by any public farmer
    endpoint — this is bank account data, not profile decoration.
    """
    farmer_id = int(get_jwt_identity())
    profile, _ = _profile(farmer_id)
    if profile is None:
        return jsonify({'error': 'No farmer profile'}), 404

    if request.method == 'GET':
        return jsonify({
            'method': profile.payout_method or 'upi',
            'upi_id': profile.payout_upi_id,
            'account_name': profile.payout_account_name,
            # Masked even to its owner: this is read on a phone in public, and
            # the farmer already knows their own account number.
            'account_number': (f'••••{profile.payout_account_number[-4:]}'
                               if profile.payout_account_number else None),
            'ifsc': profile.payout_ifsc,
        }), 200

    data = request.get_json(silent=True) or {}
    method = (data.get('method') or 'upi').strip().lower()
    if method not in ('upi', 'bank'):
        return jsonify({'error': "Choose either 'upi' or 'bank'"}), 400

    if method == 'upi':
        upi = (data.get('upi_id') or '').strip()
        # Loose on purpose: handles vary by provider and a strict pattern would
        # reject valid ones. This catches typos, not fraud.
        if '@' not in upi or len(upi) < 5:
            return jsonify({'error': 'That does not look like a UPI ID (name@bank)'}), 400
        profile.payout_upi_id = upi[:255]
    else:
        account = (data.get('account_number') or '').strip().replace(' ', '')
        ifsc = (data.get('ifsc') or '').strip().upper()
        name = (data.get('account_name') or '').strip()
        if not account.isdigit() or not (6 <= len(account) <= 20):
            return jsonify({'error': 'Enter a valid account number'}), 400
        if len(ifsc) != 11:
            return jsonify({'error': 'An IFSC code is 11 characters'}), 400
        if not name:
            return jsonify({'error': 'Enter the name on the account'}), 400
        profile.payout_account_number = account
        profile.payout_ifsc = ifsc
        profile.payout_account_name = name[:200]

    profile.payout_method = method
    db.session.commit()
    return jsonify({'success': True}), 200


# ── Farmer: redeeming ─────────────────────────────────────────────────────────

@payouts_bp.route('', methods=['POST'])
@role_required('farmer')
def request_payout():
    """Ask for the balance to be paid out."""
    farmer_id = int(get_jwt_identity())

    # Serialise every payout request for this farmer against each other.
    #
    # Without this, two taps on Redeem — or a script firing the endpoint twice
    # in the same millisecond — both run the balance and pending sums below
    # before either inserts a row, both see the full balance, and both succeed.
    # The farmer redeems ₹1,000 twice and an admin approves ₹2,000. Computing
    # "inside the transaction" is not enough: a SUM reads a snapshot, it does
    # not lock anything. The lock on the farmer's own row is what forces the
    # second request to wait, then re-read the first one's committed payout in
    # its `pending` total and find nothing left.
    lock_row(User, farmer_id)

    profile, user = _profile(farmer_id)
    if profile is None:
        return jsonify({'error': 'No farmer profile'}), 404

    method = profile.payout_method or 'upi'
    if method == 'upi' and not profile.payout_upi_id:
        return jsonify({'error': 'Add your UPI ID before requesting a payout'}), 400
    if method == 'bank' and not profile.payout_account_number:
        return jsonify({'error': 'Add your bank details before requesting a payout'}), 400

    # Read under the lock taken above, so these totals reflect every payout that
    # has actually committed rather than a stale snapshot.
    balance = wallet.balance(farmer_id)
    pending = money(db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(
        Payout.farmer_id == farmer_id, Payout.status.in_(OPEN_STATUSES)).scalar())
    available = balance - pending

    minimum = money(current_app.config.get('MIN_PAYOUT_AMOUNT', 200))
    if available < minimum:
        return jsonify({'error': f'You need at least ₹{minimum} to request a payout. '
                                 f'You have ₹{max(available, money(0))} available.'}), 400

    payout = Payout(
        farmer_id=farmer_id,
        amount=available,
        status='requested',
        method=method,
        # Snapshotted. If the farmer edits their details tomorrow, the admin
        # still pays the account that was nominated today.
        upi_id=profile.payout_upi_id if method == 'upi' else None,
        account_name=profile.payout_account_name if method == 'bank' else None,
        account_number=profile.payout_account_number if method == 'bank' else None,
        ifsc=profile.payout_ifsc if method == 'bank' else None,
    )
    db.session.add(payout)
    db.session.commit()
    return jsonify(payout.to_dict()), 201


@payouts_bp.route('', methods=['GET'])
@role_required('farmer')
def my_payouts():
    farmer_id = int(get_jwt_identity())
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'))
    query = Payout.query.filter_by(farmer_id=farmer_id).order_by(Payout.id.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(paginate_response([p.to_dict() for p in items], total, page, per_page)), 200


# ── Admin: the queue ──────────────────────────────────────────────────────────

@payouts_bp.route('/admin', methods=['GET'])
@role_required('admin')
def admin_list():
    status = request.args.get('status')
    page, per_page = clamp_page(request.args.get('page'), request.args.get('per_page'))
    query = Payout.query
    if status:
        query = query.filter(Payout.status == status)
    query = query.order_by(Payout.status == 'paid', Payout.id.desc())
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(paginate_response([p.to_dict(admin=True) for p in items],
                                     total, page, per_page)), 200


@payouts_bp.route('/admin/<int:payout_id>', methods=['PATCH'])
@role_required('admin')
def admin_update(payout_id):
    """Approve, mark paid, or reject.

    **The ledger is debited only when the money actually leaves** — on `paid`,
    not on `approve`. Approving is an intention; debiting on an intention means
    a farmer's balance drops for a transfer that might never be made.
    """
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip()

    if new_status not in ('approved', 'paid', 'rejected'):
        return jsonify({'error': "status must be 'approved', 'paid' or 'rejected'"}), 400

    # Locked before its status is read, so two admins — or one impatient
    # double-click — cannot both move the same payout to 'paid' and debit the
    # farmer's ledger twice. The second acquires the lock only after the first
    # commits, re-reads status='paid', and is turned away by the guard below.
    payout = lock_row(Payout, payout_id)
    if payout is None:
        return jsonify({'error': 'Payout not found'}), 404
    if payout.status in ('paid', 'rejected'):
        return jsonify({'error': f'This payout is already {payout.status}'}), 400

    admin_id = int(get_jwt_identity())
    payout.note = (data.get('note') or payout.note or '')[:1000] or None

    if new_status == 'paid':
        reference = (data.get('reference') or '').strip()
        if not reference:
            return jsonify({'error': 'Enter the UPI reference or bank UTR — a payout '
                                     'without one cannot be traced if it is disputed'}), 400

        # Re-checked at the moment of payment, not just at request time. A
        # refund could have reversed a credit in between, and paying out money
        # that no longer exists is not recoverable.
        if wallet.balance(payout.farmer_id) < payout.amount:
            return jsonify({'error': 'This farmer\'s balance has dropped below the '
                                     'requested amount, most likely a refund. Reject '
                                     'this and ask them to request again.'}), 400

        payout.reference = reference[:100]
        wallet.debit(payout.farmer_id, payout.amount,
                     f'Payout #{payout.id} ({payout.method.upper()})', payout_id=payout.id)
        _notify(payout, 'Payout sent',
                f'₹{float(payout.amount):.2f} has been sent to your '
                f'{"UPI" if payout.method == "upi" else "bank account"}. '
                f'Reference: {reference}')
    elif new_status == 'rejected':
        _notify(payout, 'Payout could not be processed',
                payout.note or 'Please check your payout details and try again.')

    payout.status = new_status
    payout.processed_at = datetime.utcnow()
    payout.processed_by = admin_id

    try:
        from ..utils.helpers import log_audit
        log_audit(admin_id, f'payout_{new_status}', 'payout', payout.id,
                  new_data={'amount': float(payout.amount), 'reference': payout.reference})
    except Exception:
        pass

    db.session.commit()
    return jsonify(payout.to_dict(admin=True)), 200


def _notify(payout, title, body):
    create_notification(recipient_id=payout.farmer_id, sender_id=None,
                        notif_type='payout_update', title=title, body=body,
                        data={'payout_id': payout.id})
