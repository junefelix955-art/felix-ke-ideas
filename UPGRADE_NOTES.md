# Felix KE Ideas: upgrade notes (bug fixes + Phases 1, 2 and 3)

## Fixed
1. **Site would not start**: unresolved Git merge-conflict markers (`<<<<<<< HEAD`) in `features/__init__.py` and `features/common.py`. Resolved (kept the newer side that matches your templates).
2. **Admin login 500 error** when the username contained a non-ASCII character (e.g. `é`).
3. **Duplicate usernames** differing only by capital letters (`Felix` / `felix`) could both register, which broke sign-in. Registration now checks case-insensitively.
4. **`.gitignore` was deleted** in your working copy, which risks committing `.env`. Restored (also ignores `website.db`).
5. Admin messages had **no read/unread status**; added (plus "mark all read", filter, pagination, search in message text).
6. Cleaned a dead `|safe` in `buddies.html`.

## Added (Phase 1)
- `/marketplace`, `/marketplace/<slug>`: search, category/type/price filters, sort, pagination, product pages, availability, previews, order requests with confirmation page and unique reference.
- `/learn`: learning resources (free downloads, premium previews) by topic. Same table as the marketplace, so nothing is duplicated.
- `/hire`: services, example project, request form saved to the database.
- Admin: `/admin/marketplace` (add/edit/publish/unpublish/delete, file uploads), `/admin/orders`, `/admin/inquiries` (status, private notes, email/WhatsApp/call links), overview cards on `/admin`.
- New home page and navigation (Marketplace, Learn, Hire Me, Community menu).

## Before you deploy
- Nothing new is required in `.env`. Optional: `CURRENCY` (default `KES`).
- New tables are created automatically on first start. Existing data is untouched.
- Premium files are **locked** until Phase 2 adds verified payments. Orders are requests only; "Paid" cannot be set by hand.
- Edit your own wording/prices in `features/business.py`: `STARTING_PRICE_KES`, `BUDGETS`, `EXAMPLE_PROJECTS`, `SERVICES`.
- Product files are stored in PostgreSQL. Make sure your Render database plan is persistent (not a short-lived free one) before taking real orders.
- Test locally: `py tests\phase1_smoke.py`, `py tests\phase2_smoke.py` and `py tests\phase3_smoke.py` (needs only Flask).

## Safe to delete (unused legacy files)
`static/style.css`, `static/theme.css`, `static/main.js`: the site now uses only `static/site.css`.


---

# Phase 2: accounts, orders, downloads and M-Pesa

## What was added
- **Payments** (`features/payments.py`): M-Pesa Express (STK Push). The customer gets a PIN prompt on their phone; the PIN never touches your site.
- **Verified before paid.** The callback from Safaricom is only a trigger: the server then asks Safaricom's status-query API directly and trusts only that answer. A forged callback, a browser redirect or an edited form cannot mark anything paid. The amount charged always comes from your database.
- **Never double-counted.** Each payment settles once; a second verified payment for an already-paid order is stored as "Duplicate" for you to refund, and the database refuses two "success" rows for one order.
- **Orders and downloads**: paying unlocks the file for the order link and for the signed-in buyer; every download is logged. `/account/orders` and `/account/downloads` show a member's own history only. A guest order can be added to an account with one click.
- **Receipts**: printable page at `/order/<link>/receipt`, only for paid orders.
- **Admin**: `/admin/payments` (search, status and date filters, "Re-check with M-Pesa"), `/admin/users` (customers + one-time password-reset links), verified revenue on the dashboard.
- **Password reset**: accounts have no email address, so reset is admin-issued: Admin > Customers > "Create reset link", then send the link to the member yourself (valid 60 minutes, works once).
- Safaricom's other option, **card payments, is not included**: it needs a separate provider account (for example Pesapal or Flutterwave). The payment code is isolated in `payments.py`, so it can be added later.

## Turning M-Pesa on (nothing changes until you set these)
Without them the site behaves like Phase 1 (orders handled manually). Set these in Render > Environment:

| Variable | What it is |
|---|---|
| `SITE_URL` | `https://felix-ke-ideas.onrender.com` (must be https, no trailing slash) |
| `MPESA_ENV` | `sandbox` while testing, `production` when live |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` | from your app on developer.safaricom.co.ke |
| `MPESA_SHORTCODE` | paybill/store number (sandbox: the test shortcode shown on the Daraja portal) |
| `MPESA_PASSKEY` | the Lipa Na M-Pesa passkey for that shortcode (sandbox: the test passkey on the portal) |
| `MPESA_CALLBACK_TOKEN` | a long random secret you make up (16+ characters). It becomes part of the callback URL |
| `MPESA_TILL_NUMBER` | only if you use a Till (Buy Goods): your till number |

Never put these in the code or in Git. `Admin > Payments` shows whether M-Pesa is on and which settings are missing.

## Test before real money
1. Deploy with `MPESA_ENV=sandbox` and the sandbox credentials, place an order, pay with a Daraja test phone number, and watch it flip to Paid.
2. Check `Admin > Payments` shows the receipt, and the file downloads.
3. Then apply for Safaricom's "Go Live", switch to `production` credentials, and make one small real payment yourself before announcing.
4. The automated tests use a **fake** Safaricom, so they prove the logic but not the live connection. Step 1 is what proves the connection.

## Housekeeping
- Render's logs record the callback URL (it contains your secret). If it ever leaks, change `MPESA_CALLBACK_TOKEN`.
- Money is never refunded automatically. "Duplicate" payments are for you to refund by hand through M-Pesa.


---

# Phase 3: tutoring, advertising, WhatsApp and social

## What was added
- **Online tutoring** (`/tutoring`): six subjects with learning objectives, and a request form (subject, level, online / in person / either, preferred date and time, session length, goals). Requests are saved; in `Admin > Tutoring` you can search, filter, confirm (with date and time), reschedule or cancel, and contact the student by email, WhatsApp or phone. The form is a *request*: it charges nothing and promises nothing.
- **Advertise With Us** (`/advertise`): the ways you can work with advertisers, your advertising standards, and an enquiry form (name, organisation, email, website, placement, budget range, campaign). `Admin > Advertising` lets you review each one (New, Reviewing, Approved, Declined) with private notes.
- **Sponsored labels:** tick "Sponsored" on any listing in `Admin > Marketplace` and visitors see a "Sponsored" badge on its card and page. Approved campaigns are placed by you for now (as a labelled listing); say if you want automatic ad slots later.
- **WhatsApp and social:** your Channel link is in the footer, on the home page and on the Updates page; a floating WhatsApp contact button (uses your existing `WHATSAPP_NUMBER`) appears on public pages; share buttons (WhatsApp, Facebook, X, copy link, and the phone's own share sheet) are on every product page.
- **Maths Sprint challenges:** after a game, players get a "Challenge a friend" link (`/play?challenge=NN`) that shows "A friend scored NN. Can you beat it?". The number is clamped to 0-100 so the link can't carry anything else.
- **Newsletter:** subscribe form in the footer. **This site cannot send email**, so go to `Admin > Updates > Newsletter subscribers`, export the CSV, and send from your own email tool. The CSV includes each person's personal unsubscribe link, which you should put in every email.
- **Announcements:** post news in `Admin > Updates`; it appears on `/updates` and on the home page.
- Navigation: a "Services" menu (Hire Me, Tutoring, Advertise) and an account menu (Dashboard, My orders, My downloads, Settings).

## Settings (all optional)
| Setting | Purpose |
|---|---|
| `WHATSAPP_CHANNEL_URL` | Replaces the built-in Channel link (must start with `https://whatsapp.com/channel/`) |
| `FACEBOOK_URL`, `INSTAGRAM_URL`, `X_URL`, `YOUTUBE_URL`, `TIKTOK_URL`, `LINKEDIN_URL`, `GITHUB_URL` | Each one you set (https only) appears in the footer; unset ones are hidden |

## Things for you to edit (top of `features/growth.py`)
- `LESSON_PRICE_KES`: no tutoring prices are invented; lessons show "Price on request" until you add one.
- `IN_PERSON_NOTE`, `LESSONS` (descriptions and objectives), `PLACEMENTS` and `AD_BUDGETS`: check the wording matches what you actually offer.
