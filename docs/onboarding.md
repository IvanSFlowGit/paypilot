# PayPilot onboarding

For a client engineer. Should take under 30 minutes.

PayPilot runs as **your own single-tenant deployment**. Your Stripe keys live in
your environment and are never shared with us, never sent anywhere else, and
never appear in code. There is no multi-tenant control plane and no account of
ours that can read your data.

You will do five things: create a restricted Stripe key, deploy, register a
webhook, verify a sending domain, and run one test recovery.

---

## 1. Create a restricted Stripe API key

Stripe Dashboard > Developers > API keys > **Create restricted key**.

Grant exactly these, and nothing else:

| Resource | Permission | Why |
|---|---|---|
| Billing Portal sessions | Write | Mint the card-update link the email points at |
| Invoices | Read | Optional today: invoice data arrives in the webhook payload |
| Customers | Read | Optional today: the webhook carries the name and email too |

Everything else stays **None**. PayPilot never creates charges, never issues
refunds, never modifies a subscription, and never touches card data.

Being precise, because an over-granted key is a real cost to you: at runtime the
app makes exactly **one** kind of Stripe API call, `billing_portal.Session.create`.
Everything else it knows arrives in the signed webhook payload. The two read
scopes are there for headroom as the product grows; if you want the tightest
possible key today, grant only Billing Portal write.

**The demo script needs far more than this.** `make demo-loop` creates test
clocks, customers, prices and subscriptions and pays invoices, so it will not
run on the restricted key above. Run it in a **sandbox with a full test key**
(`sk_test_...`), never with your production restricted key.

Copy the key (`rk_live_...`). You will paste it once, in step 2.

> Start in a **sandbox** with a test key (`sk_test_`/`rk_test_`) and run the
> whole of this document there first. Everything below works identically.

---

## 2. Deploy

```bash
git clone <your PayPilot repo> && cd paypilot
cp .env.example .env
```

Fill in `.env`:

```bash
STRIPE_API_KEY=rk_live_...            # from step 1
PAYPILOT_UPDATE_URL=https://yourdomain.com/billing/update  # fallback link
PAYPILOT_DB_PATH=/data/paypilot.db    # MUST be on persistent storage
PAYPILOT_PORTAL_RETURN_URL=https://yourdomain.com/billing/thanks
ADMIN_TOKEN=<a long random string>    # gates /report and /metrics
```

**The database must sit on a persistent volume.** It holds the record of which
invoices failed and which were recovered, which is what every number on the
dashboard is computed from. On ephemeral storage, a redeploy silently erases
your recovery history. On Fly, `fly.toml` already declares the mount; create it
once:

```bash
fly volumes create paypilot_data -a <your-app> -r <your-region> -s 1
fly deploy
```

Check it is alive:

```bash
curl https://<your-app>/health          # {"status":"ok"}
```

---

## 3. Register the webhook

Stripe Dashboard > Developers > Webhooks > **Add endpoint**.

- URL: `https://<your-app>/webhooks/stripe`
- Subscribe to **exactly these four events**:
  - `invoice.payment_failed` - opens a recovery
  - `invoice.paid` - closes it as recovered
  - `invoice.payment_succeeded` - closes it as recovered
  - `customer.subscription.deleted` - closes it as churn

Miss `invoice.payment_failed` and PayPilot never learns an invoice failed, so
every closing event arrives for an invoice it has no record of. Miss the closing
events and every recovery looks like a permanent failure.

Copy the endpoint's **signing secret** (`whsec_...`) into `.env` as
`STRIPE_WEBHOOK_SECRET`, then redeploy.

**Until that secret is set, PayPilot rejects every webhook with a 400.** That is
the default and it is deliberate: an unsigned endpoint that writes to a revenue
ledger lets anyone forge a recovery, choose the amount, and pick who gets
emailed. There is an escape hatch for a public demo,
`PAYPILOT_ALLOW_UNSIGNED_WEBHOOKS=1`, and you should never set it on a
deployment handling real customers.

---

## 4. Configure email

```bash
RESEND_API_KEY=re_...
PAYPILOT_FROM_EMAIL=Billing <billing@yourdomain.com>
PAYPILOT_BUSINESS_NAME=The Acme Team   # signs every dunning email
```

The From domain must be **verified in Resend**, or messages will not deliver.

`PAYPILOT_BUSINESS_NAME` is **your** business, not ours. It signs every email.
PayPilot is the tool; the recipient is your customer and has never heard of
us, so an email signed by the vendor reads as phishing. Left unset it falls
back to a neutral "The billing team".

Sending is a **dry run until you switch it on**:

```bash
PAYPILOT_SEND_EMAIL=1
PAYPILOT_ALLOWED_RECIPIENTS=you@yourdomain.com
```

`PAYPILOT_ALLOWED_RECIPIENTS` is an allowlist, and **unset means nobody**, not
everybody. Keep it to your own address until you have seen real dunning copy for
real customers of yours and are happy with it. When you are ready to go live for
all customers, set it to `*`, which has to be written out deliberately.

Recommended sequence: dry run first, read what would have gone out, then
allowlist yourself, then open it up.

Two limits apply to every send, so a Stripe retry storm cannot become an inbox
storm:

- `PAYPILOT_MAX_TOUCHES` (default 3) caps how many emails one invoice ever
  gets.
- `PAYPILOT_SEND_COOLDOWN_HOURS` (default 24) is the minimum gap between two
  emails to the same customer, across invoices.

These are deliberately separate from webhook idempotency. Idempotency stops the
same delivery being processed twice; it does nothing about three *different*,
perfectly valid events that would each mail the same person within a minute -
which is what a billing run failing three of one customer's invoices produces.

---

## 5. Verify with one real recovery

In a **sandbox**, using a full test secret key (`sk_test_...`), not the
restricted key from step 1:

```bash
PAYPILOT_DEMO_EMAIL=you@yourdomain.com make demo-loop
```

This creates a test clock, a subscription, a card that fails, advances a month
so the renewal genuinely fails, runs the recovery, fixes the card, pays the
invoice, and prints the dashboard before and after. It refuses to run against a
live key.

Then open the dashboard:

```bash
curl -H "Authorization: Bearer <your ADMIN_TOKEN>" https://<your-app>/recovery-report
```

or `https://<your-app>/report` in a browser for the human version.

---

## Optional: a holdout, so the numbers mean something

Some failed invoices recover on their own. Stripe retries them, and customers
fix their cards unprompted. Without a control group, every one of those counts
as PayPilot's win and the recovery number is unfalsifiable.

```bash
PAYPILOT_HOLDOUT_PCT=10
```

Ten percent of failures are then recorded but never messaged, and the dashboard
reports both arms side by side. Assignment is a deterministic hash of the
invoice id, so it is stable across restarts and reproducible from invoice ids
alone.

**Default is 0.** Turning it on means deliberately withholding dunning from a
share of paying customers, so it should be a decision, not a leftover setting.
Ten percent for a fixed period is usually enough; leave it off if you would
rather recover everything you can.

---

## What PayPilot will never do

- Collect or store card details. The only payment surface is a Stripe-hosted
  page.
- Send anything outside the dunning sequence, or exceed the strategy table's
  retry caps.
- Include any link in an email except your Stripe portal or invoice URL. Copy
  that fails that check is not sent at all.
- Put customer names, emails, or secrets into logs.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every webhook returns 400 | No `STRIPE_WEBHOOK_SECRET` set. Signature verification is mandatory by default. |
| Recoveries never close | Closing events not subscribed on the webhook endpoint. |
| Dashboard resets after deploy | Database is not on a persistent volume. |
| Emails log as `dry_run` | `PAYPILOT_SEND_EMAIL` is not `1`. |
| Emails log as `suppressed` | Recipient not on `PAYPILOT_ALLOWED_RECIPIENTS`, or `cooldown_active` / `max_touches_reached`. |
| Copy says "Hi there" | Stripe has no `customer_name` on the invoice. Set a name on the customer. |
| Link is not a portal URL | No billing portal configuration on the Stripe account. Falls back to the hosted invoice page. |

Logs are structured JSON on stdout. Useful events: `webhook_signature_rejected`,
`recipient_not_allowlisted`, `outbound_email_blocked`.
