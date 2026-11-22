# Authentication

Two separate things: who may open the app (Cloudflare Access), and the household's No Frills account (PC id).

## People: Cloudflare Access

Access sits in front of the app's hostname and asks for an emailed one-time code (or Google). For each
request it lets through, it adds a signed JWT (`Cf-Access-Jwt-Assertion`, also the `CF_Authorization`
cookie). `auth/access.py` verifies it on every API call:

- RS256 signature against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cached, refetched on
  rotation), audience = `ACCESS_AUD`, issuer = the team domain, not expired;
- the email is in `ALLOWED_USERS`. So a too-broad Access policy, or a request that reaches the app some
  other way, still gets nothing.

Because the Access cookie rides along on any request to the host, state-changing calls also need the
`x-vibe: 1` header (cross-site pages can't send it without a CORS preflight) and a same-origin `Origin`.
`DEV_USER` skips Access for local development, and only when the server listens on loopback.

## No Frills: PC id

PC id (`accounts.pcid.ca`) is Oracle IDCS. The app uses the PC Express Android app's OAuth client
(authorization code + PKCE, `offline_access`), as worked out by
[FireBall1725/pcexpress-mcp-server](https://github.com/FireBall1725/pcexpress-mcp-server).

- The sign-in page is behind bot detection, so a person signs in in a real browser: Settings → No Frills
  account → Sign in opens PC id in a new tab; afterwards that tab sits on
  `accounts.pcid.ca/login/success?redirectURL=com.loblaw.pcx://…?code=…` and you paste its address back.
  The app exchanges the one-time code (it only works with the PKCE verifier held by the server).
- After that, the token endpoint and the grocery API are plain HTTPS: no browser needed.
- Refresh tokens are opaque, **single-use and rotate** (reuse gives `invalid_grant: The token has already
  been consumed`). Each new one is written (AES-GCM, key derived from MASTER_KEY) before use, under a file
  lock shared by every process, in `DATA_DIR/pcid/`. Never run two copies of the app on the same tokens.
- Access tokens last an hour; they're renewed 2 minutes early, or right away when No Frills answers 401.
- If PC id is down, cart features say "try again" without signing out. If the refresh token is rejected,
  the app asks for a new sign-in. A daily job renews the session (and posts "No Frills sign-in ended" to
  Home if PC id has ended it).

The client secret is a fixed value from the app (set as `PCID_CLIENT_SECRET`, not in this repository). Using
the app's client means presenting as the official app: a terms-of-service risk for the account.
