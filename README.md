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
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> in a browser. The first server start creates `data/signals.db` with an empty `signals` table for a later stage.

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
