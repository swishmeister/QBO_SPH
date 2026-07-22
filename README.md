# QBO Quote Margin App MVP

This is a small internal FastAPI app for calculating quote margin and profit per labor hour before creating a customer-facing QuickBooks Online Estimate.

## What it does

- Connects to QuickBooks Online with OAuth 2.0.
- Caches QBO Customers and Products/Services locally for estimate building.
- Lets you build internal quotes with cost, sale price, and estimated labor hours.
- Calculates:
  - estimated revenue
  - estimated direct cost
  - gross profit
  - gross margin %
  - estimated labor hours
  - gross profit per labor hour
- Creates a QBO Estimate from approved quote lines.
- Stores detailed internal costing inside this app instead of exposing it on the QBO customer estimate.

## Important security note

If you previously pasted an Intuit Client Secret anywhere, rotate it in the Intuit Developer Portal before using this app.

This MVP stores OAuth tokens in SQLite for local testing. Before production use, encrypt token values at rest and put the app behind real authentication.

## Setup

1. Create or open your Intuit Developer app.
2. Add this redirect URI in the app settings:

```text
http://localhost:8000/qbo/callback
```

3. Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

4. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# or on Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

5. Run the app:

```bash
uvicorn app.main:app --reload
```

6. Open:

```text
http://localhost:8000
```

7. Click **Connect to QuickBooks**, then cache customers/items.

## Suggested QBO custom fields

Create these as sales form custom fields in QBO:

1. `Est Margin %`
2. `Est Profit $`
3. `Profit / Hour`

The MVP uses QBO's legacy REST custom field format for the first three string fields. If you want all four fields including `Est Hours`, add the newer GraphQL Custom Fields API in the next version.

## Workflow

1. Cache customers and items from QBO.
2. Create a new quote.
3. Add quote lines.
   - For labor lines, use quantity as estimated hours, unit cost as burdened labor cost/hour, unit price as billing rate/hour, and labor hours equal to quantity.
   - For material/sub/equipment lines, use quantity, unit cost, and unit price. Labor hours can be zero unless the line drives production hours.
4. Review margin and profit/hour.
5. Create a QBO Estimate when approved.

## Next recommended version

- Add user login.
- Encrypt OAuth tokens.
- Add estimate revision/update workflow.
- Add accepted job/project actuals comparison.
- Add import/export to Excel.
- Add GraphQL Custom Fields API support for 4+ typed custom fields.


## Render deployment

Use this start command on Render if deploying manually:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set these environment variables on Render:

```env
QBO_CLIENT_ID=your_intuit_client_id
QBO_CLIENT_SECRET=your_intuit_client_secret
QBO_REDIRECT_URI=https://sph.delightfulgardens.com/qbo/callback
QBO_ENV=sandbox
REQUIRE_BASIC_AUTH=true
APP_USERNAME=admin
APP_PASSWORD=use_a_strong_password
APP_SECRET_KEY=use_a_long_random_secret
QBO_READ_ONLY=true
DATABASE_URL=your_render_postgres_internal_connection_string
```

For production, change `QBO_ENV` to `production` and use production Intuit credentials only after sandbox testing succeeds.
