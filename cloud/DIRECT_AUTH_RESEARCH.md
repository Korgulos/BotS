# Direct EA login research — 2026-10-28

Goal: keep the existing cloud service connected to SWGoH while the desktop is off.
No unattended login or renewal has been deployed yet.

## Verified locally

- EA's current webstore login uses `SWGOH_SERVER_WEB_APP`, an authorization code,
  and an email one-time code. The verified flow is passwordless.
- Direct HTTP form submission with one user-entered code obtained an authorization
  code, plus EA `sid` and `remid` cookies. Values are kept only in ignored private
  `.state/*.local.json` files; do not commit, log, or expose these files.
- Exchanging that code at the official webstore `/auth/access_code` returned HTTP
  200. With token security enabled, the response contains an auth ID but null
  auth/refresh tokens; an opaque `authToken` cookie is set. This is not proof of
  access to game RPC or TB data.
- Reusing the remembered EA cookies obtained a fresh authorization code without
  another email code. This proves immediate silent sign-in locally, not a
  two-week lifetime, reboot recovery, or acceptance from Render's IP address.
- The `ORIGIN_JS_SDK` token flow used by upstream Lutris returned an EA access
  token with `expires_in=14399`. No scope expansion was requested.

## Tests that did not establish a game session

- Direct `AuthRpc.DoAuthByAccessCode`, using the existing PC envelope, returned
  RPC code 14 (`FEATURE_SUSPENDED`). It must not be treated as successful.
- `SWGOH_SERVER_WEB_APP` with `response_type=token` returned `unauthorized_client`
  / invalid response type. Do not assume this client supports implicit tokens.
- `AuthRpc.DoAuthNucleus` using the EA token returned RPC code 3 (`SERVER_ERROR`),
  both without and with the installed PC bundle ID. Binding was explicitly
  DONT_BIND and create-player was false. No game session was obtained.
- An earlier old-session check returned 51 (`FORCE_CLIENT_RESTART`), which is not
  equivalent to 4 (`SESSION_EXPIRED`). Do not infer actual session TTL from it.
- The current application's 24-hour cap is a local policy, not a measured EA TTL.

## Next evidence needed

Capture one successful PC `DoAuthNucleus` request and compare its exact payload,
EA token audience and envelope with the standalone attempt before making more
login attempts. A passive inspector endpoint `/api/session/auth-export` is
prepared and compiled in the ignored inspector work area. It requires the local
inspector key, same Origin, POST with `confirmed=true`, and a successful observed
login within ten minutes. It is separate from ordinary session export and bridge
observations, so auth research data is not uploaded as telemetry. The installed
DLL has a `.pre-auth-research.bak` backup.

After successful standalone game login, verify expected ally code with initial
account data and perform a read-only TB collection. Then test renewal from Render,
protect durable cookie storage, update rotated cookies, serialize refreshes, and
pause on game-session conflicts or an EA challenge. A local silent login alone
is insufficient to promise unattended operation for two weeks.

Sources: official webstore HTML/JS inspected on 2026-10-28, installed game runtime
protobuf schemas under ignored `work/direct-wire-schemas`, and upstream Lutris:
https://github.com/lutris/lutris/blob/master/lutris/services/ea_app.py

## Successful follow-up

A passive successful PC auth capture established that the nucleus token must be
JWS/JWT. Requesting `ORIGIN_JS_SDK` with `token_format=JWS` using remembered EA
cookies obtained this format without another PIN. Replacing field 1 in the
observed request and explicitly setting DONT_BIND / create-player=false produced
RPC OK, a bootstrap next-request token, a successful GetInitialData with matching
expected ally code, and an unsequenced successful Ping. Thus standalone game
login has now been verified locally. Earlier opaque-token failures above are
historical, not a current blocker. `ea_auth.py` implements the verified flow;
see README for encrypted persistence and demand-driven renewal limitations.
