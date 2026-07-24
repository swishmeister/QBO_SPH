# Upload SPH Standalone Fix

This patch removes the Save Locally dependency from the critical SPH upload workflow.

## Changes

- The estimate worksheet form now posts directly to `Upload SPH to QBO`.
- `Save Locally` is removed from the UI.
- `Upload SPH to QBO` recalculates SPH server-side from the current submitted worksheet values.
- The route updates only the configured QBO SPH custom field.
- The route does not require local worksheet edits to be saved first.
- The Add Header Row button remains removed.
- A hidden per-line calculation source is submitted so server-side math can respect Cost/Markup/Rate edits.

## Important behavior

- Labor lines matching `LC:` are used for quoted labor hours and hourly labor rate.
- Labor lines do not contribute gross material markup.
- SPH uses:

```
SPH = (Gross Markup / Quoted Labor Hours) + Hourly Labor Rate
Gross Markup = Sum of (Rate - Cost) * Qty for non-labor lines
```

## Deploy

Copy the patched files into your repo, commit/push, and redeploy Render.
