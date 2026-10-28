# SWGoH Cloud — observation and approved purchases 0.2

FastAPI + PostgreSQL, intended for the confirmed Render workspace **Swgoh bot**.
This starts the cloud part with a working historical SQL store and local uploader.
The game still needs to run to collect new observations. Approved remote shipment purchases use the running game through the local bridge.
This release does not log
into EA, send game RPCs from the cloud, execute Claims, or schedule
game actions. The existing in-game inspector continues handling approved actions.

## Data path

```text
running game / inspector DLL -> local read-only bridge -> HTTPS API -> PostgreSQL
                                                    <- read credential / queries
```

The bridge reads only the inspector's loopback GET endpoints. It never forwards
its local key, request envelopes, authentication/telemetry RPCs, raw body bytes,
headers, or inbox claim references. Source credentials are bound server-side to one
account and one device. Read and ingest credentials have separate roles. Random
credentials are stored only as SHA-256 digests in PostgreSQL. Admin bootstrap uses
a separate platform environment secret. Do not reuse EA or inspector credentials.

Stored projections can include character definitions, guild/member display names,
resource amounts, battle records and timestamps. They are private account data:
all data routes require a read/ingest key with the relevant role. OpenAPI/docs are
disabled on Render; healthz is public. Credentials can be listed by metadata and
revoked by an administrator; values are returned only during initial issuance.

## SQL tables

- `accounts`: local aliases, independent of EA account IDs.
- `sources`, `api_keys`: device identity, scoped credentials and revocation state.
- `rpc_events`: idempotent per-device/session/event observations with revisions,
  capture timestamps and bounded JSONB projections.
- `snapshots`: latest per-account/device/topic state; delayed older uploads cannot
  replace a newer snapshot.
- `shipment_offers`, `inbox_messages`, `guild_members`: relational projections linked
  to the original observation. Member ordinals are snapshot-local, not stable
  player IDs. Identical names are not assumed to identify the same person.
- `battles`: phase, type, seed, outcome and capture-completeness metadata. Start and
  Finish are not automatically asserted to match based on seed alone.

`schema.sql` is the generated PostgreSQL DDL. Startup runs version 2 additive migrations
under an advisory transaction lock. Each ingest batch commits atomically. Repeated
uploads are acknowledged without duplicate rows, including when the initial ACK
was lost. Enriched revisions update their projections in the same transaction.
The existing inspector truncation limits remain visible: a 200/435 battle capture
is stored as partial, never labeled a complete replay. Captured JSONB also keeps
sections not yet normalized into dedicated tables.

## Local setup and tests

Use Python **3.12** for the test suite; pgserver provides a temporary real PostgreSQL
16.2 instance without system installation or root access. Production uses Neon
PostgreSQL 17 and the same standard SQL/JSONB/upsert features.

```bash
python3.12 -m venv .venv-cloud
.venv-cloud/bin/pip install -r cloud/requirements-dev.txt
PYTHONPATH=cloud .venv-cloud/bin/python -m pytest cloud/tests -q
```

For local API use, set `DATABASE_URL` and a random `CLOUD_ADMIN_TOKEN` (32+ chars),
then run from this repository root:

```bash
PYTHONPATH=cloud .venv-cloud/bin/uvicorn swgoh_cloud.main:app --host 127.0.0.1 --port 18183
```

Do not commit actual `.env` files. The `.env.example` contains placeholders only.
`/healthz` verifies database connectivity. TLS is required for remote bridge uploads;
HTTP is allowed only to 127.0.0.1 for local testing. Client redirects are disabled.

## Render deployment

The root `render.yaml` defines the free `swgoh-inspector-cloud` API in
**Frankfurt**. PostgreSQL is hosted in the existing Neon `swgoh_bot` project,
branch `inspector`, database `swgoh_inspector`. Existing swgoh_api/swgoh-ui
services are separate. No game credentials are needed by this observation API.

Set `DATABASE_URL` to Neon's pooled TLS connection and
`DATABASE_URL_UNPOOLED` to the direct TLS connection for startup migrations.
Both are private Render environment variables, never source-controlled.
The previously provisioned free Render database is unused; removing its
Blueprint definition does not delete it.

Free web services can sleep after 15 minutes without inbound traffic, so the
first request can take longer. A local outbox retains pending observations
across slow responses/restarts. This is not an autonomous game worker.

1. Push code and `render.yaml` to the linked repository.
2. Create/sync the Render Blueprint; enter both Neon secrets privately.
3. Render generates `CLOUD_ADMIN_TOKEN` privately.
4. Verify `/healthz` and that unauthenticated requests cannot read data.
5. Enroll a local alias/source using the admin secret, then unset it.

The Blueprint has automatic application deploys disabled. Review future Blueprint
syncs: resource configuration changes can still trigger a deploy. SQL migration
runs at API startup rather than a paid-plan-only pre-deploy command.

The public Render JSON schema validates this Blueprint. If installing the Render
CLI, additionally run `render blueprints validate render.yaml` before applying.
Official references: https://render.com/docs/blueprint-spec,
https://render.com/docs/free, https://render.com/docs/python-version.

## Enrollment and bridge

Enrollment requires the separate admin token through `CLOUD_ADMIN_TOKEN`. It writes
one-time read and ingest credentials to mode-0600 files in gitignored `cloud/.state`;
it never prints the credentials. The admin token is not stored in the bridge config.

```bash
PYTHONPATH=cloud .venv-cloud/bin/python -m swgoh_cloud.enroll \
  --url https://YOUR-SERVICE.onrender.com --alias main --source home-pc
PYTHONPATH=cloud .venv-cloud/bin/python -m swgoh_cloud.bridge \
  --config cloud/.state/bridge.local.json --once
```

Remove `--once` to keep collecting while the game runs. The bridge reads the current
inspector URL/key on every cycle, so a game restart creates a new observation
session without manual token editing. Polling defaults to 5 seconds. It renews the
inspector's HTTP observation lease; the inspector's 250-ms sampling can still miss
short requests. It retains at most the inspector's current 200 RPC rows.

SQLite outbox/checkpoints are committed together. HTTP upload retries resend only
observations; there is no code path for retrying a game action. Outbox size is
bounded (5000 pending observations / 128 MiB); it never silently evicts unacknowledged
data. Original inspector capture timestamps remain inside snapshot payloads.
Bridge read/upload timestamps do not prove the server/game state was refreshed.

## API

Admin bearer:
- `POST /v1/accounts` with `{ "alias": "main" }` -> account ID and one-time read key.
- `POST /v1/accounts/{id}/sources` with `{ "name": "home-pc" }` -> one-time ingest key.
- `GET /v1/accounts`, `GET /v1/keys` (metadata only), `DELETE /v1/keys/{id}` (revocation).

Ingest bearer: `POST /v1/ingest` with `{ "events": [...] }`; 1–50 events, at most
1 MiB/event and 2 MiB/request. Secrets/raw transport fields and unknown RPCs are
rejected. Client-supplied account/source identity is forbidden.

Read bearer:
- `GET /v1/events?service=BattleRpc&operation=BattleFinish&limit=50&offset=0` (metadata list)
- `GET /v1/events/{id}` (one full captured projection)
- `GET /v1/snapshots`, `/v1/shipments`, `/v1/inbox`, `/v1/guild/members`
- `GET /v1/battles`, `/v1/capabilities`

## Next: standalone game RPC

Independent cloud Claims need more than the same HTTP URL. The current schema
catalog contains CLR member names, not complete protobuf wire field tags. We still
need a verified standalone request/response codec, envelope/signature/session rules,
EA login/refresh flow, and safe session-conflict behavior. The native interop DLLs
cannot simply run without the game's IL2CPP runtime. No captured EA session tokens
are uploaded by this release. Purchase authorization and the crystal-spending block
remain requirements for any future action executor.

## Web panel

Open the Render API root URL in a browser. Enter the read token or select the
private `cloud/.state/reader.local.json` file. The token is held only in tab memory,
never localStorage, cookies or the URL. The panel reads the API on the same origin
and refreshes every 15 seconds while visible. It shows all shipment offers from
the latest snapshot (with search, currency/shop filters and pagination), inbox,
energy windows, guild members, battles and individual RPC details. Shipment purchases require a separate control credential and explicit confirmation.
No Claim, refresh or admin operations are exposed. Log out before sharing the screen.

## Automatic game startup (Linux desktop)

`watch-game.py` watches this user's `SWGoH.exe` process. The installed
`swgoh-inspector-bridge.service` user service starts at login. On game detection,
it waits 20 seconds, starts one bridge and opens `open-inspector.py` once the local
server is ready. Closing the game stops the bridge; the watcher remains idle for
the next launch. A per-outbox file lock prevents manual/automatic duplicate uploaders.
No terminal, root service, paid cloud worker or game action is required.

The checked-in unit contains this desktop's paths. To disable it locally:

```bash
systemctl --user disable --now swgoh-inspector-bridge.service
```

To view startup logs (no inspector URLs or credentials are logged):

```bash
journalctl --user -u swgoh-inspector-bridge.service -f
```

## Approved remote shipments

A `control` key grants reads plus preparing/confirming one shipment purchase. The
existing `read` key remains read-only. Admins issue control keys through
`POST /v1/accounts/{account_id}/control-key`. Store the returned value privately in
`cloud/.state/controller.local.json`, then load that file in the panel.

Set `enable_purchases: true` in the private bridge config to opt that device in.
The device sends a heartbeat; stale/offline devices cannot receive confirmations.
The server selects an offer from the latest account/source snapshot, never accepts
a client-provided price, blocks PREMIUM/unknown currencies and serializes active
purchases per account. The native controller rechecks the actual offer and balance.
A 30-second quote must be explicitly confirmed in the panel before commit.

The SQLite purchase journal saves the native token locally, never to Render/Neon.
It commits a `committing` record before the native commit POST. After timeout, crash
or restart, only GET status checks are allowed, never another commit POST. Unknown
outcomes block new commands; they are not silently retried or treated as failures.
A game restart invalidates old preparations. Cloud receipt/report retries are
idempotent; completed purchases retain an audit record. No bridge listener is
exposed to the Internet and no game authentication credentials leave the PC.

## Mod selection policy (inspector 0.18+)

Shipment mod offers are hidden unless the runtime controller confirms exactly five
stars and a strictly positive SPEED stat in the secondary array. A Speed primary
does not qualify. Missing, lower/higher rarity or unverified metadata fails closed.
The original snapshot and ordinals remain intact for auditing; the panel filters
its display, and the purchase API independently rejects disallowed mods. The native
controller repeats the check before preparing and committing a purchase. Runtime
mod projections are joined by shop, item ID, refresh time and definition, not by
parsing a display ID or guessing stats. No mod is sold, deleted or purchased by
this filtering. Restart the game after installing inspector 0.18.0.

### Inbox Claim preko panela

Control ključ može da pripremi jednu podržanu rang nagradu iz trenutnog inbox snimka. Bridge proverava isti snimak i priloge u lokalnom inspectoru. Native reference i approval token ostaju lokalno. Panel prikazuje tačne priloge i traži zasebnu potvrdu u roku od 30 sekundi. Primanje PREMIUM je dozvoljeno; trošenje crystals ostaje blokirano. Dismiss, posebne poruke, kuponi i entitlement tokovi nisu podržani. Jedan zajednički red serijalizuje kupovine i Claim; nepoznat ishod blokira sledeće akcije. Posle prekida/restarta proverava se status i ne ponavlja native commit. Nova native DLL nije potrebna (postojeće inbox Claim rute); restart bridge-a učitava novi worker.

Inspector 0.19.0 dodaje Claim za tačno proverene parove tip/naslov: PARAMETERIZED + RAID_REWARD_SUBJECT_KEY, GUILDRANKEDREWARD + GUILD_CHALLENGE_REWARD_SUBJECT_KEY, TERRITORYWARCOMPLETEREWARD + TERRITORY_WAR_SECOND_PLACE_SUBJECT_KEY. Ostale PARAMETERIZED poruke ostaju blokirane. Važe iste provere priloga, vremena, posebnih tokova i eksplicitne potvrde. Potreban je restart igre posle zamene DLL.

### Direct connection diagnostics

`POST /v1/direct/probe` (control key) issues one credential-free HEAD to the fixed SWGoH RPC URL from the API host. Redirects are disabled, TLS verification remains enabled, and upstream headers/body are not returned. `transport=https_response` means only an HTTP response was received, including rejection codes; `authenticated=false` and `rpc_sent=false` remain explicit. This is not a login, a game Ping or an action executor. No game tokens are uploaded. The next standalone work requires wire schemas and a verified account/session flow; reflected CLR property lists are insufficient.

`POST /v1/direct/metadata-probe` (control key) sends one credential-free ContentRpc.GetMetadata protobuf request to the fixed RPC endpoint. Tags come from the installed runtime model. A bounded minimal codec decodes the response code and public version fields; headers, session tokens, config values and raw response bodies are never returned. Success requires RPC code 1 (OK), not merely HTTP 200. No login, account read or game action is performed. There are no automatic retries or arbitrary RPC/target parameters.

### Existing-session local proof and temporary cloud reads

Manual local helper: `PYTHONPATH=cloud work/cloud-pg-venv/bin/python -m swgoh_cloud.local_session capture`, then close the game and run `... local_session ping`, then `... local_session initial-data`. Private session and selected account projection stay in ignored `cloud/.state` files owned by the current user with mode 0600. The helper requires the game to be stopped. No credentials are printed; no purchases, claims, binding or token refresh operations exist. A separate explicit flag allows at most one diagnostic retry of the read-only initial-data call for a verified session with no request-token sequencing. Network uncertainty otherwise blocks replay. Local decompressed account data is bounded to 32 MiB; only selected inventory/profile/inbox metadata is saved, excluding identity tokens and raw envelopes.

The temporary cloud route `POST /v1/direct/account/read` accepts only Ping/GetInitialData and `game_closed=true`, with the existing control key. The service variable `SWGOH_DIRECT_TEST_SESSION` is optional and secret; never put its value in Git, Blueprint, logs or observations. It binds one session to one account/source, expires within 30 minutes of capture, and supports only verified absence of request-token sequencing. Recent online bridge status blocks it. Each operation reserves an audit event before network I/O; uncertain/failed requests are never replayed, and completed reads return their saved result. GetInitialData requires a successful direct Ping and validates the expected ally code. This does not implement EA login, credential renewal, or off-game purchases/claims. Clear the temporary secret after the proof. Existing bridge-based purchases/claims remain separate.

### Session validity checks

Inspector 0.23.0 adds a read-only in-game session check through the existing AuthSessionService, plus runtime schemas for AuthSessionActive and the access-code/refresh response types. `POST /api/session/check` requires the existing inspector key, same Origin, JSON, and `confirmed=true`; `GET /api/session/check/status` shows only callback state and an active flag. It never invokes logout or renewal. The direct local helper additionally supports `session-check` (AuthRpc.IsSessionActive, verified empty request, response bool field 1). It still refuses to send while SWGoH is running. Compare this boolean with a successful Ping rather than inferring an exact lifetime. No session-expiry field is present in AuthSessionActiveResponse. Schema presence for DoAuthByRefreshToken is not proof that the PC Nucleus login obtains a refresh token; its response fields include a refresh-token expiry, but this project does not invoke it.

For the separate EA-app-closed condition, after a successful initial direct Ping, the local helper permits one manual `ping --phase ea-app-closed` and one `session-check --phase ea-app-closed`. The phase is journaled before sending and cannot repeat or proceed after uncertainty. It still refuses game-active probes, does not log out of EA, and cannot invoke any renewal or mutation operation. `capture_age_seconds` measures time since the captured Ping, not the true session creation time or expiry.

### Local direct shipment trial

`swgoh_cloud.local_purchase` is a manual local-only trial, not a Render endpoint or background worker. It requires a fresh direct initial-data snapshot, an explicit expected ally code, a prepared quote and separate confirmation within two minutes. Only fixed-price Credits offers are eligible. It checks account, price, availability and balance again before one send. A durable journal records sending before network I/O; unknown outcomes cannot be retried. The tested purchase was 2 × `001` for 800 Credits with SWGoH closed, verified by a fresh purchased flag and balance decrease of exactly 800. Cloud purchase buttons still use the game bridge.

### Render panel: bounded direct Credits purchases

The panel can prepare and confirm Credits purchases through `/v1/direct/purchases/prepare` and `/v1/direct/purchases/{id}/confirm` while the game bridge is offline. Configure a private `SWGOH_DIRECT_TEST_SESSION` Render secret with the already verified account/source binding, fresh captured session, random `_testId`, `_expiresAt` no later than capture + 30 minutes, and `_allowPurchases: true`. The panel displays this application-imposed expiry; it is not the known server lifetime. No automatic renewal or credential ingestion endpoint is enabled. Session values are never sent to the browser or saved in the purchase journal.

Direct requests reserve the shared SQL purchase journal before I/O, recheck account/offer/price/balance before confirmation and before sending, and never resend a purchase after an uncertain outcome or server restart. Existing game bridge commands remain separate. Crystals, money, mods and other currencies are not allowed through this first direct implementation. SQL schema is unchanged. A successful direct purchase marks the matching saved offer purchased. Prepare and cancel can be verified in production without spending resources.

### Direct currencies and success dialog (current)

Direct shipments now accept every known runtime `CurrencyType` except PREMIUM (crystals) and REAL_MONEY. Names and numeric wire IDs live in `currency_policy.py`; balances and response debit checks use the selected currency. All 16 verified shipment types are supported. Prices must remain fixed and positive, offers active and unpurchased, with no unverified purchase requirement. Mod purchases require a verified 5-star definition from the saved game snapshot and a positive Speed **secondary** from the fresh server reply. Unknown or unverified mod previews remain blocked.

On a newly completed purchase the dialog displays “Transakcija uspela” for 1.4 seconds, then closes and refreshes the panel. Errors and uncertain outcomes stay open; reopening successful history does not auto-close. Native inspector 0.25.0 has the same expanded currency allowlist. Existing session expiry and explicit confirmation requirements remain unchanged.

### Inventory and owned quantities

Inventory snapshots contain only definition-level currencies, materials, equipment, units and mods, with decimal-string quantities (including Int64 precision) and capture time. Instance IDs and identity/authentication fields are excluded. Equipped and unequipped mods are grouped by mod definition; units are grouped by base definition. `GET /v1/inventory` and snapshot reads are account-scoped. `POST /v1/inventory/refresh` requires a control key and a valid offline direct session; it does not buy or claim anything.

Direct shipment preflight and a best-effort post-purchase read save fresh inventory; failure after a confirmed purchase marks the inventory stale without changing the purchase result or repeating it. Inspector 0.26.0 captures Inventory from GetInitialData through `/api/inventory`, and the bridge preserves original capture time so an old game snapshot cannot replace a newer direct snapshot. Game-observed resource-changing operations mark the cached inventory as needing refresh.

The Inventory page supports category filtering and search. Shipments show “U inventaru” by exact resource type + definition, with mod/unit canonicalization. Missing rows mean zero only in a complete captured category; unknown categories or missing source snapshots display a dash.

### Opt-in automatic inbox rewards

`POST /v1/inbox/automation` (control key, enabled boolean) persists an account/source-bound policy in the existing event store; GET reads it. The bridge tick requests `/v1/bridge/inbox/auto-prepare` only while online with actions enabled. The default is off. Enabled mode validates the same exact type/subject whitelist and fixed supported attachments as manual Claim, obtains and rechecks the native quote, and authorizes the existing single-send journal path without another UI confirmation. Purchases remain manual.

A stable visible reward fingerprint prevents duplicate automatic attempts when rows are renumbered. Ambiguous identical previews, special or unverified live details, expired rewards, unsupported types, and any prior matching attempt are skipped. Unknown sends block retries; disabling cancels only unsent automatic commands and is rechecked at commit reservation.

Inspector 0.28.0 reads current messages from the existing Inbox controller without sending refresh RPCs. It preserves opaque local handles, checks known detail types and action flags, and leaves unverified live details blocked. Successful Claim dialogs now show the receipt and close after 1.4 seconds, just like purchases. Automatic Claims do not open a confirmation popup; their outcomes appear in inbox history.

### Territory Battle phase history

Inspector 0.30.0 exports `/api/territory`: a bounded, passive projection of the guild roster,
map status and already-decoded GetMapStats. Map instance and member IDs are represented
only by stable, opaque hashes. GetMapStats is associated only when its typed request map
matches an observed TB instance; unrelated maps are ignored.

The bridge uploads these observations with topic `territory`. The existing SQL event store
keeps the raw snapshot and one checkpoint per source, TB instance and phase (1–6), with no
schema migration. `/v1/territory/phases` requires a read/control credential and is account
isolated. Live checkpoints update in place; a later-phase response containing explicit
historical round statistics or a final TB result confirms a final checkpoint. Merely
advancing the map closes the last-seen checkpoint without claiming it is a final result.
Old observations cannot downgrade a final checkpoint. Earlier phase roster/GP data is
retained when later historical results finalize that phase.

The guild panel joins all roster members to the available round and zone statistics.
Missing rows stay unknown instead of zero. Covert mission states preserve UNATTEMPTED,
ATTEMPTEDFAILED and ATTEMPTEDSUCCEEDED with attempted/completed flags. Combat wave counts
do not imply completion of a whole mission. Undeployed GP is a derived comparison to the
captured guild roster, which may differ from a TB-locked eligible roster. No final phase
results can be collected while both the game and bridge are unavailable; the panel marks
such gaps instead of fabricating results. Phase changes follow server rounds, not local
midnight or an assumed 24-hour schedule.

### Direct Territory Battle reads

The control-only `POST /v1/territory/collect` reads the authenticated account's
GetInitialData, GuildRpc.GetGuild, TerritoryRpc.GetTerritoryMapStatuses and
TerritoryRpc.GetMapStats (explicit TB event type 9). It joins the complete guild
roster and writes the existing six-phase checkpoints. No battle, purchase,
claim, authentication renewal or raw credential export is performed.

The **Učitaj TB sa servera** button is available in **Gild i borbe** with a control
key. The game must be offline; overlapping collections and immediate repeats
within 60 seconds are skipped. Read keys can inspect
`GET /v1/territory/collection` but cannot trigger collection.

`SWGOH_TERRITORY_SESSION` is a private JSON environment secret bound to an
account and source, with an explicit expiration capped at 24 hours from capture.
Rejected sessions disable the schedule; expired sessions require fresh capture.
Do not commit this value. Schema tags in `territory_schema.json` were exported
from the game's RuntimeTypeModel using inspector 0.31.0.

Hourly collection code is prepared in `python -m swgoh_cloud.territory_job`,
but **no hourly infrastructure is enabled**. The user selected manual collection
and declined creating the paid Render cron service. A future cron service must
be explicitly provisioned and receive its own approved configuration; the free
API process is not used as a reliable in-process timer.

### Local hourly maintenance timer

`swgoh-maintenance.timer` runs at boot (after about two minutes) and hourly via
`swgoh-maintenance.service`. `Persistent=true` catches a missed calendar tick;
user lingering lets the user manager start at boot, before graphical login.
No paid Render cron service is created.

The timer calls `POST /v1/maintenance/run` using the existing private local
control configuration. The server reads TB statistics and inventory, ACCEPTs
only the already allowlisted fixed-reward inbox messages, and calls the verified
empty `PlayerRpc.ClaimAllEnergyRewards` request for free quest energy. It never
sends Dismiss, purchases, currency spending or authentication renewal.
The game must be offline. The session secret has the existing 24-hour cap.
The local tick uses one stable request ID per account/UTC hour; the server keeps
per-message and per-energy-hour mutation receipts. A send with an unknown outcome
is not repeated. Fresh readback updates inbox/inventory and may reconcile absent
message IDs or changed bonus-energy timestamps.

Inspect/stop/restart the user timer:

```bash
systemctl --user status swgoh-maintenance.timer
journalctl --user -u swgoh-maintenance.service -n 30
systemctl --user disable --now swgoh-maintenance.timer
systemctl --user start swgoh-maintenance.service
```

`GET /v1/maintenance/status` is account-scoped and read-key accessible.
Credentials and raw inbox IDs are not written to receipts, panel responses or
service logs. The protected local `.state/maintenance-last.local.json` records
the last result, while the system journal records each timer execution.

### Public guild TB table

`/tb` serves a token-free, read-only guild table; `/public/tb/phases` returns
only the allowlisted TB table fields for `SWGOH_PUBLIC_TB_ACCOUNT`. This explicit
server-side account setting is required (404 when disabled); URL parameters
cannot select another account. Public records contain member names and TB
statistics, but omit account/source IDs, internal member keys, event metadata,
inbox, inventory and session credentials. The page sends only public GET reads;
phase selection, sorting and details are local presentation controls. Opening
it never triggers a SWGoH request. All existing action routes still require a
control key. Both the page and data response request no search indexing.

Events are ordered by their start date, newest first. This uses the actual
server start when available, otherwise the existing marked estimate from a
phase end. Public presentation is copied from the shared table block in
`panel/app.js`; the JS test checks that the blocks match so later table changes
must also update `panel/public-tb.js`.

### Managed EA sessions (demand-driven)

The existing API can now renew its TB/maintenance session without the desktop.
`SWGOH_EA_LOGIN_SEED` is a Fernet-encrypted bootstrap document; the separate
`SWGOH_AUTH_ENCRYPTION_KEY` is a private Render secret. Neither belongs in Git or
Blueprint values. The seed binds one account, source and expected ally code to
verified client settings and remembered EA cookies. It contains no cached game
session. Provision these secrets only after a user completes the EA email-code
login; no account password is stored.

`direct_auth_sessions` stores encrypted rotated cookies and the verified cached
game session, independently of public observations. The existing startup
migration creates this additive table. Keep the encryption key across deploys;
rotating it requires reseeding. A changed seed ID explicitly replaces the prior
bootstrap; the same seed ID never overwrites rotated cookies on restart.

On demand, the server requests an EA JWS token, performs `DoAuthNucleus` with
DONT_BIND and create-player=false, consumes its bootstrap request token with
GetInitialData, verifies the expected ally code, and verifies unsequenced Ping.
Cached sessions last at most three hours and renew before expiry. Account locks
serialize renewals; recent online bridges and active transactions block login.
Authentication failures are persisted with a ten-minute backoff and are not
retried inside the same request. A rejected game session invalidates its cache.
EA may still revoke remembered login or request an interactive challenge.

Read/control `GET /v1/territory/session` returns only state and timestamps.
Control `POST /v1/territory/session/renew` requires `{"game_closed":true}` and
returns only public status; immediate force repeats use the recent cached result.
TB collection and maintenance obtain managed sessions automatically. Existing
manual environment sessions remain the fallback when managed EA login is not
configured for the requested account. Purchase session configuration is unchanged.

This is demand-driven renewal, not an hourly scheduler. With the desktop off,
use the server's control endpoints. No paid cron or new hosting is provisioned.
