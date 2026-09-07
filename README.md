# Killshot FX License & Logic Server

A small server that (1) validates license keys with hardware-lock, and
(2) does sensitive calculations (like martingale lot sizing) server-side,
so that logic never ships inside the bot.py your clients run.

## Deploying to Railway

1. Create a free Railway account at railway.app, connect your GitHub.
2. Push this folder (`server.py`, `requirements.txt`, `Procfile`) to a
   **private** GitHub repo (a different one from your bot's update repo -
   keep them separate).
3. In Railway: New Project → Deploy from GitHub repo → select this repo.
4. Railway auto-detects it's a Python app and deploys it using the `Procfile`.
5. In your Railway project's Variables tab, set:
   - `SERVER_SECRET` - any long random string (used to sign tokens)
   - `ADMIN_SECRET` - a different long random string (protects key issuance)
   - Leave `DB_PATH` unset (defaults to `licenses.db` in the app's storage)
6. **Important:** by default, Railway's filesystem is not persistent between
   deploys - your SQLite database would reset every time you redeploy. Go to
   your service → Settings → Volumes → add a volume mounted at `/data`, then
   set the `DB_PATH` variable to `/data/licenses.db`. This keeps your issued
   licenses safe across deployments.
7. Once deployed, Railway gives you a URL like `https://your-app.up.railway.app`.
   Test it by visiting that URL in a browser - you should see
   `{"status": "Killshot FX license server is running"}`.

## Issuing a license key to a new client

Once deployed, issue keys with a simple request (replace the URL and secret):

```powershell
Invoke-RestMethod -Uri "https://your-app.up.railway.app/admin/issue-key" -Method Post -ContentType "application/json" -Body '{"admin_secret": "YOUR_ADMIN_SECRET", "days": 30}'
```

This returns a new `license_key` like `KILLSHOT-4025-01E2-59FB` - send that
to your client along with the bot files.

## Wiring it into the bot

1. Put `license_client.py` in the same folder as `bot.py`.
2. In `license_client.py`, set `LICENSE_SERVER_URL` to your actual Railway URL.
3. In `config.py`, add a new field for the client's license key:
   ```python
   LICENSE_KEY = "PASTE_LICENSE_KEY_HERE"
   ```
4. In `bot.py`'s `main()` function, before starting the bot, call:
   ```python
   import license_client
   ok, msg = license_client.ensure_licensed(config.LICENSE_KEY)
   if not ok:
       print(f"License check failed: {msg}")
       return  # don't start the bot
   ```
5. For the martingale sizing specifically, `do_place_martingale_range()`
   in `bot.py` would call `license_client.get_martingale_sizing(...)`
   instead of computing `lots` locally - ask if you want this wired in
   directly, since it means removing the formula from `bot.py` entirely.

## Testing before going live

The server has already been tested locally end-to-end (key issuance,
activation, hardware-lock enforcement, and the sizing calculation) - see
the test output from when this was built. Before using it for real:

1. Deploy to Railway following the steps above.
2. Issue yourself a test key.
3. Point a test copy of the bot at your deployed URL and confirm
   `ensure_licensed()` succeeds.
4. Try activating that same key from a second "fake" hardware ID (you can
   temporarily hardcode a different value in `get_hardware_id()` for this
   test) and confirm it's correctly rejected.

## What this does NOT do yet

- **No payment integration.** Keys are issued manually via the admin
  endpoint - there's no automatic connection to Stripe/LemonSqueezy/etc.
  yet. That would be the next piece if you want fully automated sales.
- **No license revocation UI.** To deactivate a key, you'd currently need
  to connect to the database directly (`UPDATE licenses SET active = 0
  WHERE license_key = '...'`) - a simple admin page could be added later.
- **Only the martingale sizing is protected as an example.** Any other
  logic you want moved server-side (OCO margin sizing, risk calculations,
  etc.) would follow the same pattern - ask if you want more moved over.
