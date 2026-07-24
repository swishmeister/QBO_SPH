# Upload to QB NameError Fix

This patch fixes the Upload to QB crash:

`NameError: name 'db' is not defined`

Cause: the SPH calculation function used the database session to resolve item/labor/variable-cost rules, but the function did not accept `db` as a parameter.

Fix:
- `calculate_sph_from_submitted_quote_form` now accepts `db` explicitly.
- Upload to QB passes the active SQLAlchemy session into the function.
- No QuickBooks logic or estimate upload behavior was changed.

Replace:
- `app/main.py`

Then redeploy Render.
