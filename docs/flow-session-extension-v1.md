# Google + Flow session collector v1

## Status and scope

Extension version: **1.3.0**. The managed package adds import-only enrollment.
The Waoowaoo receiver is deployed as **v0.4.233**; Banana consumption and the old
Veo HTTP adapter are not changed or certified by this extension update.

Packaged artifact: `.artifacts/flow-session-extension-1.2.1.zip` (ten extension files,
no profile or credentials) is the previous manual-setup package. Version 1.2.0
cannot authenticate to the secured receiver. Use the private 1.3.0 package below
for the requested no-configuration rollout, not the old archive or public source.

## Managed internal deployment (1.3.0)

Import the private `Hidemium-Flow-Auto-1.3.0.zip` into Hidemium and open the profile.
No URL change, key entry, enable toggle or manual Sync is needed. First startup
enrolls the profile, enables cookie-only Google + Flow sync to
`https://nathanai.xyz`, and opens one background Flow tab if absent. Once the
authenticated page is ready, collection and authenticated upload happen
automatically. Subsequent starts preserve an explicit user disable. Concurrent
startup hooks do not create duplicate Flow tabs.

The profile must already have a usable Google/Flow login. A signed-out account,
consent screen or identity challenge still needs the account owner's action;
importing an extension cannot create a valid login. CAPTCHA-task mode, automatic
reload and browser-task polling are not enabled by managed enrollment.

The private package contains a collector credential. Keep it inside the trusted
operator group; anyone with it can authenticate uploads. It is not a consumer
lease credential and must never be published to GitHub. Public
`managed-config.js` deliberately contains `null` and retains manual enrollment.
The collector API remains authenticated, with encrypted server storage.

Build the private archive outside this checkout:

```sh
node scripts/package-managed-flow.cjs --key-file /private/collector.key --output /private/new-package
```

The default collector is `https://nathanai.xyz`. The packager rejects output inside
the checkout, existing destinations and invalid keys/origins, and makes the ZIP
and embedded config readable only by the current OS user. The private key must
match the receiver's `FLOW_COOKIE_SYNC_KEY`.

### Production automatic-import proof

On September 10, 2026 (Asia/Ho_Chi_Minh), the exact packaged extension bytes were
loaded from a fresh extension path into the user's already-logged-in profile via
Banana's Cloak wrapper. There were zero manual settings calls, zero manual Sync
calls and zero generation requests. Fresh import and browser restart each synced
sixteen cookies for the expected identity, with one Flow tab and no credential
in settings responses. Production encrypted storage revision increased from
4 to 6 after import and to 7 after restart; the restart check required a new
sync timestamp rather than trusting persisted status.

All 56 extension tests pass. Private metadata-only evidence is
`/Volumes/1tb/Hidemium-Flow-Auto-1.3.0/automatic-import-proof.json`; delivery archive is
`/Volumes/1tb/Hidemium-Flow-Auto-1.3.0/Hidemium-Flow-Auto-1.3.0.zip`.
This proves automatic capture/transport/restart for this account on this machine,
not Windows Hidemium bulk import, all accounts, session longevity or video output.

The approved live experiment showed that the combination of fourteen Google-parent
authentication cookies and two Flow cookies restores the manually logged-in account
in a clean profile. Neither group alone worked in that experiment. The collector
therefore exports the tested allowlist, not the full Google cookie jar. Sixteen is
a tested sufficient set, not a proven universally minimal set.

## Source files

Under `tool_veo_3_extracted/tool_veo_3-restore-3592172/captcha-solver-main/src/modules/captcha/extension/`:

- `flow-session.js`: scope/attribute validation, stable snapshot, identity checks,
  single-flight coordination, destination/consent recheck, twenty-second deadline.
- `flow-session-chrome.js`: real Chrome cookie-store and Flow-tab adapter, v1 transport.
- `background.js`: integration, trusted popup commands, per-profile/per-origin
  consent, no fallback from selected Flow mode into legacy upload.
- `popup.html` / `popup.js`: explicit opt-in, metadata-only Check and Sync controls.
- `manifest.json`: version and explicit Flow/Google host entries.

## Manual public-source workflow

1. Load/reload the unpacked extension from the extension directory, or extract the
   supplied ZIP into a new directory and load that directory. Do not assume that
   merely restarting an existing browser updates its cached service-worker code.
2. Open the already-authenticated Flow page. The collector does not navigate or
   reload that page, nor does cookie mode inject the CAPTCHA worker.
3. Set Cookie mode, receiver origin, and the dedicated collector key configured as
   `FLOW_COOKIE_SYNC_KEY` on the receiver (32–512 characters). The key is stored only
   in local storage restricted to trusted extension contexts, never sync storage.
   Leaving the password field empty preserves the key only for the same origin.
   Changing origin requires a new key. Enable the extension and explicitly
   select **Đồng bộ phiên Google + Flow (v1)**, then save.
4. **Kiểm tra phiên Flow, không gửi cookie** validates the live identity and cookie
   snapshot and returns only status/count. It does not send a bundle.
5. **Đồng bộ phiên Flow** performs receiver capability negotiation before collecting
   for upload. Missing consent, unsupported receivers and unstable sessions stop
   without falling back to the old Labs cookie endpoint.

The opt-in is backed by a local origin-bound consent record. A synced mode setting
alone cannot authorize a different browser profile or a newly changed destination.
The existing legacy cookie mode remains available when Flow mode is not selected;
it must not be interpreted as proof of new Flow readiness.

## Receiver contract for the next project phase

Only HTTPS or loopback HTTP origins are accepted. Embedded URL credentials, query
strings, fragments and non-root paths are rejected. Redirects are rejected.

`GET /api/cookie-sync/flow` must return a successful JSON response with
`protocol: "flow-session-v1"`. Both GET and POST require `X-Extension-Key`.
Compatibility negotiation alone is not authentication. Missing key stops before
cookie reads or requests; changing the key during capture aborts upload.

`POST /api/cookie-sync/flow` receives:

```json
{
  "protocol": "flow-session-v1",
  "session": {
    "account": "verified-flow-account@example.test",
    "origin": "https://flow.google.com",
    "capturedAt": "2026-09-09T19:00:00.000Z",
    "cookies": []
  }
}
```

The actual cookie array must contain the sixteen validated records. Each preserves
`name`, `value`, `domain`, `path`, `secure`, `httpOnly`, Chrome `sameSite`, `hostOnly`,
`session`, `storeId`, and `expirationDate` for non-session cookies. Cookie-store IDs
identify the source snapshot; a consumer maps into its own isolated target store.
Partitioned, mixed-store, duplicate, incomplete, near-expired and unexpected-scope
records fail closed. No Labs token/cookie or Grok state is included.

A successful POST response must explicitly return
`{"protocol":"flow-session-v1","accepted":true}`. An arbitrary HTTP 200 is not
reported as success. The old `/api/cookie-sync` must NOT accept or silently ignore
this new envelope as if it were the legacy contract.

Before production support, the receiver must add authenticated enrollment/upload,
authorization, encrypted storage, retention/revocation and independent identity
validation after restoration. The extension's protocol handshake does not supply
those server-side protections. Do not enable remote collection before this work.

## Validation performed

Automated command from repository root:

```sh
node --test tests/extension-*.test.cjs
```

**48 tests passed**, covering pure bundle rules, account/cookie/destination changes,
timeout/late work, single-flight and check-vs-sync contention, receiver negotiation,
Chrome-store selection, ambiguous accounts, popup sender authorization, profile-local
consent, legacy-fallback prevention and disabled/cookie-mode side effects.
All extension JavaScript files pass `node --check`; `git diff --check` is clean.

Real browser proof uses the exact manually logged-in source profile, not a synthetic
cookie fixture. The extension package is a byte-for-byte copy of the current source,
loaded via Banana's existing Cloak persistent-profile launcher with `extension_paths`.
Its content digest is:

`f785b8779811c2d39f5767594c019deaca9fd51882cc057c8be43fef9d75f199`

The immutable unpacked path avoids a stale service-worker cache discovered during
validation. The live worker also explicitly proves the local-consent code is loaded.

- Disabled collector performs no credential POST, including after the startup timer.
- Legacy/unsupported receiver response is rejected without a credential POST.
- Twenty consecutive real metadata-only captures pass: all sixteen cookies valid.
  Median capture time 3.75 ms, maximum 6.4 ms on the already-loaded local profile.
  This excludes browser launch, network upload and restoration, and is not a fleet benchmark.
- Three simultaneous real sync requests produce exactly one POST to the loopback
  receiver. Every caller receives an acknowledged success.
- No captured cookie values are present in extension local storage.
- The popup page loads and its actual runtime message reaches the protected handler.
- Only the POST payload from the real extension is imported into an independent
  clean profile. That profile restores the expected Flow identity after import and
  after closing/reopening the browser. No manual login, whole-profile copy or
  localStorage copy is used.
- Original source identity remains authenticated. Collection is disabled after testing.
- No generation request, credit expenditure, video download, production upload,
  account-session lease, deployment, commit or merge is part of this extension test.

Sanitized live evidence:
`/Users/server/workspace/banana_tool/tool_veo3_mau/.runtime/extension-flow-proof-1788981684.json`

Diagnostic harness: `/tmp/hidemium-real-extension-proof.py`.
Cookies remain in memory and private browser profiles, never in this document,
the ZIP package or raw test logs.

## Limits

### Authenticated receiver proof (1.2.1)

The real extension was loaded on the same manually authenticated source profile,
then sent its bundle directly to Waoowaoo `npm run dev` on loopback port 3200.
Twenty check-only captures passed; three concurrent sync calls produced one stored
revision. Anonymous discovery was rejected. The database held encrypted data and
legacy Labs accounts remained unchanged. A separate, explicitly synthetic local
machine/account lease authorized retrieval of the real bundle; that returned
bundle restored the correct identity in a clean Cloak profile before and after
restart. Wrong machine, expired/released lease, stale upload and revoked bundle
were rejected. Test fixtures were removed and source collection disabled afterward.

Sanitized evidence:
`/Users/server/workspace/banana_tool/tool_veo3_mau/.runtime/flow-server-real-proof-1788983515.json`.
This is real cookie transport/restore proof, not video generation or deployed-server
proof. That earlier local test preceded the v0.4.233 production release and the
managed-package production proof described above.

- This is same-machine, short-duration portability proof for the user's account.
  It does not certify other accounts, cross-OS/IP transfers, expiry recovery or load.
- Collection reads identity from the authenticated Flow UI and checks stability
  during capture. A future receiver/consumer still must independently verify the
  restored identity; source UI metadata is not a server authentication credential.
- Background suspension/network ambiguity can prevent acknowledgement even after
  receipt. There is no automatic POST retry; the receiver must support safe idempotent
  upsert before scheduled production use.
- A provider cookie contract change fails closed rather than expanding collection
  scope or adding retries automatically.
- Waoowaoo storage/transport now has local integration proof; Banana application
  consumption remains a separate integration phase. Do not call this three-project
  E2E or a complete Veo-generation fix.
