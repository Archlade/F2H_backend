-- Remind customers before their weekly basket is prepared.
--
-- A standing basket is easy to forget about. By the time the delivery is
-- generated the order is confirmed and its stock is committed, so a customer
-- who is away that week has already cost a farmer a picked basket. The reminder
-- goes out before that point, while pausing or editing still costs nobody
-- anything.
--
-- One column, and it exists to stop double-sending. The reminder job runs from
-- cron and may run more than once a day — a retry, a manual trigger, two hosts.
-- Recording which delivery date was last reminded about means the second run
-- finds its own work already done and sends nothing. A reminder that arrives
-- three times is worse than one that arrives late.
--
--   mysql -u f2h -p f2h < database/basket_reminders.sql

ALTER TABLE family_pack_subscriptions
  ADD COLUMN last_reminded_for DATE NULL AFTER last_generated_date;

-- Existing baskets start with NULL, so each gets exactly one reminder for its
-- next delivery and settles into the weekly rhythm from there.
