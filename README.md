# ADWILAGO Trading Signals

Stage 2 adds a read-only Exness market-data integration to the Stage 1 FastAPI dashboard:

- The backend signs Exness requests with Ed25519.
- The Gateway host is used to discover the account-specific Access Point.
- The account and available instruments are verified before requesting data.
- Genuine USDJPY M5 candles are retrieved and rendered in the existing chart.
- A documented Exness ticks WebSocket is used for live bid/ask updates when available, with reconnect backoff and resubscription.
- Historical M5 candles are loaded once, then reconciled with a small overlapping request rather than re-downloading the full history on every dashboard refresh.
- REST candle status and tick WebSocket status are reported separately; stale candle data is marked visibly.
- Stage 3 calculates a 75-period close SMA with 9-period SMA smoothing and a 7-period Wilder RSI with 9-period SMA smoothing from completed candles only.
- No trading-operation endpoint or strategy engine is implemented.

The application still intentionally has these restrictions:

- The initial signal state remains **No Signal**.
- No strategy engine or automated trading is implemented.
- Any future trade execution remains manual through Exness Terminal.

## Run locally on Windows

From this project directory, create and activate a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start the development server:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080> in a browser. The application uses the configured `data/signals.db`; preserve this database and its history during maintenance.

### Daily production startup

Use one PowerShell terminal opened from an ordinary, user-launched VS Code session. Do not start production from Codex's managed execution terminal: that environment was observed to provide process-level proxy variables pointing to a loopback endpoint that refused connections. Automatic reload is intentionally disabled for normal operation so one strategy engine runs in one server process:

```powershell
Set-Location "F:\adwilago trading signals"
Remove-Item Env:SSLKEYLOGFILE -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Keep that terminal open. The server is restricted to this laptop at `127.0.0.1:8080`. Press `Ctrl+C` in the same terminal to shut it down gracefully; wait for the application shutdown-complete message before closing the terminal. Before restarting, verify that port 8080 has been released with the listener check below. Restarting the application invalidates in-memory login sessions, so sign in again afterward.

The proxy diagnosis found `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` in the Codex-managed process environment, targeting `127.0.0.1:9`; no service was listening there. Windows user and machine proxy environment variables were unset, WinINet proxying was disabled, and WinHTTP was configured for direct access. The ordinary VS Code PowerShell terminal reported all 12 upper- and lowercase proxy variables unset, and market connectivity recovered when the application was started there. The precise mechanism that supplied the managed process variables was not established. Do not disable proxy, antivirus, or TLS security controls based on this observation.

If login succeeds but the chart is blank and REST or tick status reports an error, authentication may be working while upstream Exness connectivity is failing. Check the authenticated market-data status and server logs separately. The market-data API can return HTTP 200 with an error status in its JSON payload, so HTTP status alone does not establish market-data health. A login rejection is a separate local authentication issue.

### Development startup with reload

For code development only, use:

```powershell
Set-Location "F:\adwilago trading signals"
Remove-Item Env:SSLKEYLOGFILE -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

On this laptop’s Python 3.15.2 and Uvicorn 0.34.0, the managed execution terminal reported `PermissionError: [WinError 5]` while Python’s Windows multiprocessing resource-sharing code created a named pipe. The application worker still served requests, but the reload supervisor emitted repeated errors. Use the daily no-reload command for reliable operation; if reload is needed, run it in an interactive VS Code PowerShell terminal and confirm startup completes without those errors.

### Check whether the server is running

```powershell
$listeners = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
if ($listeners) {
    $listeners | Select-Object LocalAddress, LocalPort, OwningProcess
} else {
    "Adwilago is not listening on port 8080."
}
```

For the local-only configuration, `LocalAddress` should be `127.0.0.1`. If a listener already exists, do not start another application instance. Stop the one in its terminal with `Ctrl+C` first.

## Configure Exness locally

Copy `.env.example` to `.env` and fill it locally using values from Exness Key Management. Never commit or paste the file contents.

```env
EXN_API_KEY=your_api_key_identifier
EXN_PRIVATE_KEY=your_ed25519_private_key
EXN_ACCOUNT_ID=123456789
EXN_DISCOVERY_BASE_URL=https://the-gateway-host-from-the-exness-dns-widget
```

`EXN_ACCOUNT_ID` must be the numeric Exness trading account ID, not a login label or account alias. The application signs requests using either Exness's 128-character hex private key or an unencrypted Ed25519 PEM key. The dashboard reports `Disconnected` or `Error` without fabricating market values when configuration or API access is unavailable.

The backend endpoint is `GET /api/market-data`. It returns the cached, authenticated candle snapshot; credentials never reach the browser. The dashboard also connects to the backend-only `WebSocket /ws/market-data` endpoint for tick updates. The backend connects to Exness's signed tick stream and does not expose Exness credentials to the browser.

Run the smoke test:

```powershell
pytest
```

## Project structure

`app/main.py` is the FastAPI entry point and dashboard route. `app/database.py` creates only the future signal-history table. The HTML template is in `app/templates/dashboard.html`; styling and browser behavior are separated into `static/css/style.css` and `static/js/dashboard.js`. `tests/test_app.py` verifies that the main page returns the expected Stage 1 state.
