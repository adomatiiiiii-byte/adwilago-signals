# Caddy hotspot reverse proxy preparation

This directory contains a **non-deployable template**, not an active Caddy
configuration. Caddy is not started by this preparation. The FastAPI backend
remains on `127.0.0.1:8080`.

## Template and required values

`Caddyfile.example` requires all four process environment variables shown at
its top. A future launcher must refuse to start if any are empty, malformed,
or not freshly verified against the active personal Android hotspot:

- `ADWILAGO_LAPTOP_IPV4`: current laptop hotspot IPv4.
- `ADWILAGO_PHONE_IPV4`: current phone source IPv4 as observed/verified on this
  hotspot. Do not infer it from the gateway address.
- `ADWILAGO_CADDY_STORAGE`: absolute path
  `%LOCALAPPDATA%\Adwilago\Caddy\data` after expanding `%LOCALAPPDATA%`.
- `ADWILAGO_CADDY_LOG_FILE`: absolute path
  `%LOCALAPPDATA%\Adwilago\logs\caddy-access.json` after expanding the
  variable.

The two IP values must be individual IPv4 addresses, not CIDRs or subnet
ranges. The known historical hotspot addresses are not defaults and must not
be copied into runtime configuration without re-verification. Do not put the
active Caddyfile, storage, certificates, keys, logs, or runtime scripts in the
repository. `.gitignore` excludes common accidental additions under this
directory.

## Security behavior in the template

- The site is explicitly HTTPS on TCP `8443`; `bind` limits the socket to the
  laptop IP. There is no wildcard bind or HTTP site.
- `auto_https disable_redirects` disables the extra HTTP-to-HTTPS redirect
  listener. `protocols h1 h2` omits HTTP/3, so no QUIC/UDP listener is enabled.
- `tls internal` selects Caddy's local issuer. `skip_install_trust` prevents
  Caddy from installing its root into the laptop's system, Firefox, or Java
  trust stores. A future Android trust step must be explicitly approved and
  must transfer only the public root certificate, never CA storage/private
  keys.
- `admin off` disables Caddy's administration API. Configuration changes will
  require a controlled stop and restart.
- `remote_ip` matches the actual direct TCP peer against one exact phone IPv4.
  No Caddy trusted-proxy ranges are configured. The fallback handler returns
  403; it never proxies unauthorized peers.
- Caddy's `reverse_proxy` preserves the incoming `Host` (needed by the app's
  host allowlist) and normally generates `X-Forwarded-For`,
  `X-Forwarded-Proto`, and `X-Forwarded-Host`. It ignores client-supplied
  values for those three headers unless trusted proxies are configured. This
  template does not configure any trusted proxies and strips `Forwarded` and
  `X-Real-IP`. The app must continue to authenticate independently and must
  not treat forwarding headers as credentials.
- WebSocket upgrade forwarding is built into Caddy's `reverse_proxy`; no
  separate WebSocket route or alternate authentication is introduced.
- Access logs are written outside the repository. Caddy redacts credentials
  in access logs by default; protect the logs because they still contain
  request metadata and client addresses.

## Existing application compatibility

The FastAPI authentication middleware validates the raw `Host` for every
HTTP/WebSocket route, validates browser `Origin` for WebSockets and
state-changing operations, protects APIs and static files, and requires the
server-side session cookie. Login uses a one-use CSRF token and logout checks
the session CSRF token. WebSockets require a valid session and Origin and are
closed on session expiry/revocation. Caddy must preserve the browser's
`Origin`; the phone URL's origin must be configured in
`DASHBOARD_PHONE_ORIGIN` before activation.

Use the exact eventual phone origin, including `https://` and `:8443`, for
`DASHBOARD_PHONE_ORIGIN`. The app's allowed Host is derived from this setting,
so the original Host forwarded by Caddy must match it. No wildcard Host or
Origin is appropriate. Existing localhost HTTP origins remain allowed for
local access.

For HTTPS cookie behavior, Uvicorn must accept forwarded protocol information
only from loopback. The planned server command should explicitly include
`--proxy-headers --forwarded-allow-ips=127.0.0.1`. Caddy connects to the
backend over loopback and supplies the HTTPS scheme. Do not use `*` or a LAN
range in `--forwarded-allow-ips`. This is an IP-level trust boundary: any
process on this Windows account/machine can also connect from loopback, so
forwarded headers must never be used for authentication or authorization.
The app's current authentication and origin checks do not use them as identity.

The dashboard uses same-origin `/api/...` fetches and derives `ws:` versus
`wss:` from the page URL, so it needs no hardcoded backend URL change for this
proxy. The browser will connect to
`wss://<verified-laptop-ip>:8443/ws/market-data`. FastAPI's docs/OpenAPI routes
and protected static files pass through the same app authentication
middleware; Caddy adds no Basic Auth layer.

## Network safety and startup design

No current network values are recorded here. Before any future activation, a
per-user PowerShell launcher should, while Caddy is stopped:

1. Identify the actual connected Wi-Fi SSID (not just the Windows profile
   display name), interface alias/index, Private category, IPv4, prefix and
   gateway; verify internet access and the intended Android hotspot.
2. Confirm the phone's source IP from a fresh, controlled connection or other
   reliable phone-side evidence. The gateway is not proof of the phone source.
3. Confirm `127.0.0.1:8080` is the only application bind and that the expected
   app instance is running. Never start a second FastAPI/strategy process.
4. Validate every address and the exact `.env` phone origin; set only
   process-scoped Caddy variables; adapt the config; then start Caddy on the
   explicitly bound address. The later Windows Firewall rule must be limited
   to TCP 8443, the verified local address, exact phone remote address, Wi-Fi
   interface, and Private profile.
5. Continue monitoring the active SSID, adapter, category, local IP and gateway
   while access is enabled. On any mismatch, immediately stop Caddy and fail
   closed. Never follow a network/IP change automatically. The firewall rule
   must independently remain narrow so a monitor delay cannot expose another
   interface.

The eventual machine-specific locations are:

- `%LOCALAPPDATA%\Programs\Caddy\caddy.exe`
- `%LOCALAPPDATA%\Adwilago\Caddy\Caddyfile`
- `%LOCALAPPDATA%\Adwilago\Caddy\data`
- `%LOCALAPPDATA%\Adwilago\logs`
- `%LOCALAPPDATA%\Adwilago\scripts`

The runtime directories are not created by this preparation. When created,
the storage directory must inherit/retain ACLs granting full access only to
the dedicated Windows user, SYSTEM and Administrators; remove broad
`Users`/`Everyone` write/read access and verify the resulting ACL. It contains
the CA private keys and must never be copied to the phone or repository. Logs
should be user-readable only. The phone receives only the public root CA
certificate through a separately reviewed, fingerprint-verified procedure.

## Configuration checks

`caddy adapt --adapter caddyfile --config deploy/caddy/Caddyfile.example`
parses/adapts a Caddyfile to JSON; it does not start listeners or provision
certificates. For a syntax-only check, supply temporary RFC 5737 TEST-NET
addresses and a nonexistent temporary storage/log path in the process
environment. Do not use current or historical hotspot addresses. Do not run
`caddy validate`, `caddy run`, `caddy start`, or `caddy trust` during this
preparation. Adaptation is not a runtime test: later validation must be planned
so that it cannot create certificates/storage, install trust, open listeners,
or alter the production app/database.

## Frontend dependencies

The dashboard self-hosts the **Lightweight Charts 4.2.0 standalone production
build** at
`static/vendor/lightweight-charts/4.2.0/lightweight-charts.standalone.production.js`.
It was obtained from the official npm package
`https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-4.2.0.tgz`;
the exact pinned UNPKG build was byte-for-byte identical. Verified npm package
integrity: `sha512-TRC61cI6bEKBEHcpqawL0KP8Rwos9b2OcdyeQ4Ri7Zybf8NvrRYmNcJV+2a2rMo7VZp1NhVWURuNEskjc+wm4A==`.
Verified JavaScript SHA-256:
`46FC69534EC098F095BBCD1D9A26D693D39A8B9EEFF7343536765B3DD28C2BDF`.

The vendor directory includes the package's Apache-2.0 `LICENSE` and the
upstream v4.2.0 `NOTICE` text. Keep the TradingView attribution logo/link
visible; the existing chart configuration leaves the library's default
attribution enabled. Do not modify the vendor JavaScript.

For a future upgrade, review the new pinned version and its upstream release,
obtain the exact package and matching pinned distribution from official
sources, verify the archive against npm `dist.integrity`, compare the archived
JavaScript member byte-for-byte with the pinned distribution, and record its
SHA-256. Review and include applicable license/NOTICE files, then update the
versioned path and its regression-test digest. Review chart API and behavior
changes separately before adopting a new version.

The page still loads Google Fonts CSS from `fonts.googleapis.com` and font files
from `fonts.gstatic.com` (DM Sans and Space Grotesk). These provide typography
and do not execute JavaScript, but require external browser requests. Self-host
them in a separately reviewed change if offline operation or eliminating those
requests is required; typography may change if they are removed without local
replacements.
The backend Exness REST/WebSocket connections remain outbound backend traffic
and are not affected by self-hosting browser assets.

FastAPI's default Swagger UI and ReDoc pages also load UI JavaScript and styles
from `cdn.jsdelivr.net`. The JavaScript executes on authenticated documentation
pages. These routes remain protected by application authentication, but require
external browser access unless those UI assets are self-hosted or the routes
are disabled in a separately reviewed change.

## Future certificate and test sequence

Before activation, separately approve and plan: setting the exact
`DASHBOARD_PHONE_ORIGIN`; generating the internal CA and leaf certificate only
after the runtime path/ACL review; verifying the leaf certificate SAN contains
the exact laptop IP; exporting only the public CA root; checking its SHA-256
fingerprint locally and on the transfer channel; and manually installing that
root on the Android device. Never approve browser certificate warnings.

Future security checks should cover: config adaptation with TEST-NET values;
no listener during adaptation; valid and invalid Host/Origin; spoofed
`Forwarded`/`X-Forwarded-*`; allowed and denied remote peers; authenticated
HTTP/API/static/docs/WebSocket access; WebSocket expiry; HTTPS Secure cookie;
localhost HTTP login; hotspot mismatch causing shutdown; university Wi-Fi
fail-closed behavior; certificate SAN/trust; firewall scope; and rollback by
stopping Caddy and removing the narrowly scoped firewall rule. Existing tests
use temporary SQLite databases and mocked/no-op lifespans; they must continue
to run separately from the production server and database.

No reverse proxy is active, no firewall rule is present, and no application,
credential, database, or strategy change is part of this preparation.
