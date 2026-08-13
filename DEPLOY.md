# Deploying the backend

## The thing that catches people out

`backend/` is its **own GitHub repository** — `Archlade/F2H_backend` — nested
inside `Archlade/F2Hmarket`. The parent tracks it as a gitlink (a bare commit
pointer), not as ordinary files.

**Pushing `F2Hmarket` does not push one line of backend code.** It pushes a
pointer, and if the backend commit was never pushed to its own remote, that
pointer refers to something nobody else can fetch.

This is how the cart shipped to the website and 404'd on the server: the React
code went up with the parent repo, `app/routes/cart.py` stayed on the laptop,
and `/api/cart/items` did not exist on the API.

So: **two repos, two pushes.** Every time.

```bash
cd ~/Desktop/F2H/backend && git add -A && git commit -m "..." && git push
cd ~/Desktop/F2H         && git add -A && git commit -m "..." && git push
```

## Is a thing actually deployed?

Ask the server, don't assume. The health endpoint is public:

```bash
curl -s https://api.f2hmarket.com:8443/api/health
```

For a specific route, look at the status code rather than the body — `404` means
the code is not there, `401` means it is there and simply wants a login:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.f2hmarket.com:8443/api/cart
# 401 = deployed    404 = not deployed
```

## Deploying

**1. Push the backend.**

```bash
cd ~/Desktop/F2H/backend
git add -A
git commit -m "Cart, weekly baskets sold by F2H, pickup payments, cron reminders"
git push origin main
```

**2. Pull on the server.**

```bash
ssh <your-vps>
cd /srv/webapps/farmapp
git pull
```

**3. Run any migrations you have not run yet.**

They live in `database/` inside this repo, so they arrive with the pull. Each
one is additive and safe to run on live data, but none of them are idempotent —
running one twice errors on the duplicate column rather than corrupting
anything.

Check what is already applied before running:

```sql
-- cart.sql
SHOW TABLES LIKE 'cart_items';
-- farmer_paid_at_pickup.sql
SHOW COLUMNS FROM payments LIKE 'farmer_paid_at';
-- basket_sold_by_f2h.sql
SHOW COLUMNS FROM family_pack_orders LIKE 'hold_reason';
-- basket_reminders.sql
SHOW COLUMNS FROM family_pack_subscriptions LIKE 'last_reminded_for';
```

Then run whichever came back empty:

```bash
mysql -u f2h -p f2h_db < database/cart.sql
mysql -u f2h -p f2h_db < database/farmer_paid_at_pickup.sql
mysql -u f2h -p f2h_db < database/basket_sold_by_f2h.sql
mysql -u f2h -p f2h_db < database/basket_reminders.sql
```

`farmer_paid_at_pickup.sql` and `basket_sold_by_f2h.sql` alter an ENUM, which
makes MySQL rewrite the table and hold a lock. Run those at a quiet hour.

**4. Restart.**

```bash
sudo systemctl restart farmapp     # whatever the unit is called
curl -s https://api.f2hmarket.com:8443/api/health
```

**5. Confirm the thing you deployed is actually there.**

```bash
curl -s -o /dev/null -w 'cart: %{http_code}\n' https://api.f2hmarket.com:8443/api/cart
```

`401` is success here. `404` means step 1 or 2 did not take.

## Scheduled work

Nothing in this app runs on a timer by itself. Weekly deliveries are generated
opportunistically when somebody opens a page, and basket reminders do not go out
at all without cron.

Once `CRON_TOKEN` is set in `.env.production`, add one line to `crontab -e`:

```
0 6 * * * curl -fsS -X POST https://api.f2hmarket.com:8443/api/cron/run -H "X-Cron-Token: YOUR_TOKEN" >> /var/log/f2h-cron.log 2>&1
```

`-f` makes curl fail loudly on an HTTP error, so a broken job reaches cron's
mail instead of logging "fine" forever.
