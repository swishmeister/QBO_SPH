# QBO SPH Calculator and File Editor — Estimate Library Build

Internal FastAPI app for QuickBooks Online. It imports QBO estimates, calculates SPH from gross markup, and manages item purchase/sale pricing without replacing QuickBooks as the accounting system.

## Production URL layout

- Host domain: `sph.delightfulgardens.com`
- Launch URL: `https://sph.delightfulgardens.com/`
- Connect/Reconnect URL: `https://sph.delightfulgardens.com/qbo/connect`
- Disconnect URL: `https://sph.delightfulgardens.com/qbo/disconnected`
- OAuth Redirect URI: `https://sph.delightfulgardens.com/qbo/callback`

## Major features in this build

### Estimate Library

- Import all current-year QBO estimates from January 1 forward.
- Refresh the estimate library quickly.
- Search the local estimate library by customer name or estimate number.
- Import older estimates one at a time by estimate number.
- Open each estimate in a spreadsheet-style worksheet.

### Spreadsheet Estimate Editor

Visible columns:

```text
Product/Service | Description | Qty | Cost | Markup % | Rate | Amount | Delete
```

Line math:

```text
Amount = Qty × Rate
Gross Markup = (Rate - Cost) × Qty
Markup % = (Rate - Cost) / Cost
Rate = Cost × (1 + Markup %)
```

SPH math:

```text
SPH = (Gross Markup ÷ Quoted Labor Hours) + Hourly Labor Rate
```

The SPH summary at the top of each estimate updates live in the browser as worksheet values change.

### Variable-cost material-code handling

Generic non-inventory material codes such as `MC`, `MI`, `MP`, and `MM` are treated as variable-cost placeholders.

Rules:

- The app ignores the QBO item-list default price/cost for these items during estimate import.
- The app uses the actual QBO estimate line rate/amount from the transaction.
- On first import, Cost is set equal to the estimate Rate so markup starts at 0%. The row is highlighted so the user knows to replace Cost with the real purchase price.
- On later refreshes, the app preserves a designer-entered cost unless the cost is still zero.
- These items are locked out of Item Price Manager uploads.

Configure the code list with:

```env
VARIABLE_COST_ITEM_CODES=MC,MI,MP,MM
```

### Upload SPH to QBO

The app can upload the calculated SPH value to the SPH custom field on the linked QBO estimate.

```env
QBO_CF_SPH_ID=3
QBO_CF_SPH_NAME=SPH
```

Keep `QBO_READ_ONLY=true` until you are ready to allow uploads.

### Item Price Manager

- Import QBO items.
- Search/filter item library.
- Edit purchase price, markup %, and sale price locally.
- Review pending price changes before upload.
- Upload only `PurchaseCost` and `UnitPrice` to QBO.
- Does not edit quantity on hand.

## Recommended production environment variables

```env
QBO_ENV=production
QBO_CLIENT_ID=<Intuit production client ID>
QBO_CLIENT_SECRET=<Intuit production client secret>
QBO_REDIRECT_URI=https://sph.delightfulgardens.com/qbo/callback
QBO_READ_ONLY=true

QBO_CF_SPH_ID=
QBO_CF_SPH_NAME=S.P.H
VARIABLE_COST_ITEM_CODES=MC,MI,MP,MM

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
- This build defaults to `QBO_READ_ONLY=true`. That means importing, editing locally, and calculating work; QBO uploads are blocked.
- Set `QBO_READ_ONLY=false` only after production import testing is complete. The main estimate action is **Upload to QB**, which updates QBO estimate line Product/Service, Description, Qty, Rate/Amount, and the S.P.H custom field. The app Cost column remains internal and is not written to QBO estimate lines.

## Production deployment steps

1. Push this build to the private GitHub repo.
2. In Render, verify `PYTHON_VERSION=3.12.8`.
3. Verify the start command is:
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
4. Use the Render Postgres **internal** database URL for `DATABASE_URL`.
5. Set `QBO_ENV=production` and production Intuit credentials.
6. Keep `QBO_READ_ONLY=true` for the first production test.
7. Add `https://sph.delightfulgardens.com/qbo/callback` to Intuit's Production Redirect URIs.
8. Reconnect QuickBooks from `https://sph.delightfulgardens.com/qbo/connect`.
9. Import several real estimates and confirm the math before enabling writes.

## Local development

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

For local development, set `SECURE_COOKIES=false` and use sandbox credentials.


### Labor item rule

Labor service codes are detected by the `LABOR_ITEM_PREFIXES` setting. The default is `LC:` so items like `LC:MA Labor maintenance` and `LC:PL Planting` are treated as labor. On import, their quantity is counted as quoted labor hours and their rate is used as the hourly labor rate. Labor line cost is set equal to rate so labor contributes to quoted hours/rate but does not inflate gross item markup.

### SPH custom field safety

Set the SPH custom field by name, not by a guessed DefinitionId:

```env
QBO_CF_SPH_NAME=S.P.H
QBO_CF_SPH_ID=
```

The SPH uploader now matches the custom field by name first and will stop with a detailed error instead of writing to another field when the DefinitionId is stale or wrong.


### Upload to QB

The estimate screen button is now **Upload to QB**. It pushes the current worksheet to the linked QuickBooks estimate.

It updates these QuickBooks estimate fields:

- Product/Service
- Description
- Qty
- Rate
- Amount
- `S.P.H` custom field

It does **not** write the app's Cost column to QuickBooks estimate lines. Cost is internal to the SPH worksheet and is used only to calculate gross markup and SPH. Existing QBO line details such as tax/class refs are preserved when they exist on the latest QuickBooks line.

### Product/Service auto-fill

When a Product/Service is selected from the worksheet dropdown, the app now fills the line description, quantity, cost, rate, markup, and hidden QBO item id from the local item cache. Refresh the Item Price Manager after deploying so item descriptions and prices are cached from QuickBooks.

New rows added inside the app will upload to the linked QuickBooks estimate as long as the Product/Service was selected from the cached dropdown. The app sends Product/Service, Description, Qty, Rate, Amount, and S.P.H. The Cost column remains internal to this app.

## Upload variable-cost and blank-line behavior

- MC, MI, MP, and MM estimate lines are uploaded to QuickBooks as normal Product/Service estimate lines.
- Variable-cost rows import with Cost equal to Rate and Markup 0% so they are visibly flagged as needing the real purchase cost.
- Once the Cost is changed from the imported Rate, the pink warning flag is removed while the Cost cell remains editable.
- The app's Cost column is not sent to QuickBooks; it remains internal for SPH math.
- Blank separator rows and rows with only a Description upload to QuickBooks as DescriptionOnly lines.

## Time Charge Cleanup

The application includes a bulk QuickBooks Online `TimeActivity` cleanup page at:

```text
/time-charges
```

The page can scan and filter time activities by date, customer/job, employee or vendor, service item, description, and billable status. Selected records must be exported to CSV before deletion. Records marked `HasBeenBilled` are protected and cannot be deleted through this feature.

Before each delete batch, the app re-reads the selected TimeActivity records from QuickBooks. A record is skipped if it was deleted elsewhere, became billed, or its `SyncToken` changed after the scan and backup. Delete requests are sent in batches of 10 and logged in the application database.

Keep this Render variable enabled while testing the scan and CSV workflow:

```env
QBO_READ_ONLY=true
```

After verifying the records shown by the scanner, change the Render variable to:

```env
QBO_READ_ONLY=false
```

Render must redeploy after the environment-variable change before permanent deletion is enabled.
