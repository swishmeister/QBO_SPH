# QBO Quote Margin App — Production Build

Internal FastAPI app for importing QuickBooks Online Estimates, adding internal cost/labor assumptions, and calculating line markup, quote margin, SPH, and profit per hour.

## Production URL layout

- Host domain: `sph.delightfulgardens.com`
- Launch URL: `https://sph.delightfulgardens.com/`
- Connect/Reconnect URL: `https://sph.delightfulgardens.com/qbo/connect`
- Disconnect URL: `https://sph.delightfulgardens.com/qbo/disconnected`
- OAuth Redirect URI: `https://sph.delightfulgardens.com/qbo/callback`

## Recommended production environment variables

```env
QBO_ENV=production
QBO_CLIENT_ID=<Intuit production client ID>
QBO_CLIENT_SECRET=<Intuit production client secret>
QBO_REDIRECT_URI=https://sph.delightfulgardens.com/qbo/callback
QBO_READ_ONLY=true

REQUIRE_BASIC_AUTH=true
APP_USERNAME=<admin username>
APP_PASSWORD=<strong password>
APP_SECRET_KEY=<long random secret; do not change after connecting QBO>
SECURE_COOKIES=true
ENABLE_HSTS=true

DATABASE_URL=<Render Postgres internal database URL>
PYTHON_VERSION=3.12.8
```

## Important security notes

- Do not commit `.env`, database URLs, Intuit client secrets, or Render secrets to GitHub.
- `APP_SECRET_KEY` is used for session signing and token encryption-at-rest. Once production QBO is connected, changing `APP_SECRET_KEY` will make encrypted stored tokens unreadable and require reconnecting QuickBooks.
- This build defaults to `QBO_READ_ONLY=true`. That means users can import estimates and calculate metrics, but the app will not create new QBO estimates.
- Set `QBO_READ_ONLY=false` only after production import testing is complete.

## Production deployment steps

1. Push this build to the private GitHub repo.
2. In Render, set `PYTHON_VERSION=3.12.8` and verify the start command is:
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
3. Use the Render Postgres **internal** database URL for `DATABASE_URL`.
4. Set `QBO_ENV=production` and production Intuit credentials.
5. Keep `QBO_READ_ONLY=true` for the first production test.
6. Add `https://sph.delightfulgardens.com/qbo/callback` to Intuit's Production Redirect URIs.
7. Reconnect QuickBooks from `https://sph.delightfulgardens.com/qbo/connect`.
8. Import one real estimate and confirm the math before enabling writes.

## Local development

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

For local development, set `SECURE_COOKIES=false` and use sandbox credentials.
