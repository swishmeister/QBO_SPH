# Refresh Variable Settings Fix

This patch fixes the Refresh From QBO internal server error caused by a deployment mismatch where `main.py` expected `settings.variable_cost_item_codes` but the deployed `config.py` did not define it.

Files included:
- `app/main.py`
- `app/config.py`
- `.env.example`

Render environment variables to confirm:
- `VARIABLE_COST_ITEM_CODES=MC,MI,MP`
- `LABOR_ITEM_PREFIXES=LC:`

After applying, redeploy Render and use Refresh From QBO again.
