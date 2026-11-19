# Felix KE Ideas Marketplace – Phase 1

This update adds a first Marketplace implementation to the existing Flask/PostgreSQL project.

## Included
- Public `/marketplace` catalogue with search and category filtering.
- Public product detail pages.
- Order request form and confirmation reference.
- Admin listing management at `/admin/marketplace` (uses the existing administrator login).
- Add, edit, publish/unpublish and delete listing actions.
- Admin order request review and status updates.
- Additive PostgreSQL tables created by the existing `features.init_schema()` startup path.
- Marketplace links in the main navigation, footer and existing admin dashboard.

## Important payment limitation
This phase records an order request only. It does not collect money, verify payments, or mark orders as paid. `payment_status` remains `unpaid`. M-Pesa/card integration and server-side payment verification belong in Phase 2.

## Setup and local testing
1. Back up the current project folder before replacing any files.
2. Copy the updated project files into a separate local test copy.
3. Install dependencies from `requirements.txt` in your existing virtual environment.
4. Configure the same required environment variables in your local `.env` (do not upload or share that file).
5. Run the Flask app locally. When `DATABASE_URL` is configured, the existing startup database initialization creates the new Marketplace tables using `CREATE TABLE IF NOT EXISTS`.
6. Sign in through the existing administrator login and open `/admin/marketplace`.
7. Add a draft listing, publish it, test the public catalogue, then submit a test order request using your own test contact details.
8. Confirm the order appears in the admin Marketplace page and that changing its order status does not change its payment status.

## Product images
The first version accepts an HTTPS image URL. It does not upload image files. Use images you own or have permission to use. Image upload/storage can be added later with validated file handling.

## Deployment
Do not deploy until local testing succeeds. Commit and push the tested files to the repository connected to Render. The database schema is additive and does not delete existing tables or records, but take a database backup before deployment as a normal precaution.
