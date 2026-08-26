"""What customers think of F2H itself — the app, the site, the service.

Deliberately not the `reviews` table. That one is about a *product* or a *farm*:
it carries `product_id` and `farmer_id`, it feeds `rating_avg` on those rows,
and a review there is written by somebody who bought a specific thing. This is
about the service as a whole, written by anybody with an account, and it feeds
the homepage rather than a product page. Sharing a table would have meant every
existing query learning to exclude a kind of row it was never written for.

**One per customer, edited in place.** A second submission updates the first
rather than adding another. Nobody needs two opinions of the same service from
the same person on a homepage, and without this the obvious way to get your
review published is to keep submitting until one gets through.

Editing sends it back to the queue — see `is_approved` below.
"""

from datetime import datetime

from ..extensions import db


class ServiceReview(db.Model):
    __tablename__ = 'service_reviews'

    id = db.Column(db.Integer, primary_key=True)

    # Unique: the upsert in `POST /service-reviews` keys on it, and the
    # constraint is what makes "one per customer" true rather than merely
    # intended. Two requests racing would otherwise both insert.
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True)

    rating = db.Column(db.SmallInteger, nullable=False)
    comment = db.Column(db.Text)

    # Nothing reaches the homepage without an admin saying so. The box is open
    # to every account, so this is the only thing standing between a bad day and
    # the front page.
    #
    # An edit resets it to False. A review that was approved as praise must not
    # be quietly rewritten into something else while keeping its place on the
    # homepage — that is the one way an approval queue can be worked around.
    is_approved = db.Column(db.Boolean, nullable=False, default=False, index=True)

    approved_by = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    approved_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    author = db.relationship('User', foreign_keys=[user_id])
    approver = db.relationship('User', foreign_keys=[approved_by])

    def to_dict(self, include_author=True):
        data = {
            'id': self.id,
            'rating': self.rating,
            'comment': self.comment,
            'is_approved': self.is_approved,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_author and self.author:
            # First name and initial only on anything that can be published.
            # A full name plus a review is more than somebody agreed to put on a
            # public homepage, and the admin queue has the whole record anyway.
            last = (self.author.last_name or '').strip()
            data['author'] = {
                'display_name': f"{self.author.first_name} {last[:1]}.".strip()
                                if last else self.author.first_name,
                'avatar_url': self.author.avatar_url,
                # Customers and farmers both leave feedback, and the two read
                # very differently on a homepage — "the vegetables were fresh"
                # against "F2H sells what I grow". Published so the page can
                # say which it is instead of calling everyone a customer.
                # A role, not personal information.
                'role': self.author.role_name,
            }
        return data

    def to_admin_dict(self):
        """The full record, for the approval queue only."""
        data = self.to_dict()
        data['user_id'] = self.user_id
        data['full_name'] = self.author.full_name if self.author else None
        data['email'] = self.author.email if self.author else None
        data['approved_by'] = self.approver.full_name if self.approver else None
        data['approved_at'] = self.approved_at.isoformat() if self.approved_at else None
        return data
