# Felix KE Ideas: upgrade notes (bug fixes + Phase 1)

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
- Test locally: `py tests\phase1_smoke.py` (113 checks, needs only Flask).

## Safe to delete (unused legacy files)
`static/style.css`, `static/theme.css`, `static/main.js`: the site now uses only `static/site.css`.
