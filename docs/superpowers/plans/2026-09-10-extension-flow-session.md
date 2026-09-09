# Extension Flow Session Implementation Plan

**Goal:** Export the proven Google + Flow cookie bundle from the logged-in profile, without mixing Labs identity or sending credentials to a legacy receiver.

**Approved scope:** Extension only. No changes to Waoowaoo, Banana source or production. Preserve existing uncommitted work and the user's source profile. No commit or deployment.

**Architecture:** A pure allowlisted cookie contract, a single-flight capture/sync coordinator, and a Chrome API adapter. Existing cookie mode gains an explicit opt-in for the new Flow protocol. No raw cookie persistence in extension storage or messages. Flow identity comes from an authenticated Flow tab, never a Labs token or URL hint.

**Patterns considered:** (1) append Flow-domain cookies to the old payload: rejected by the live negative control and legacy receiver incompatibility; (2) export all Google cookies/profile state: rejected as excessive credential scope; (3) opt-in, versioned, allowlisted bundle with identity/snapshot validation: selected. No new infrastructure or retry loops.

**Budgets:** One in-flight sync per service worker, one capture per trigger; 20-second overall deadline, bounded network requests, at most sixteen allowlisted cookies and 64 KiB serialized payload. No automatic retries. Cookie expiry must have at least sixty seconds remaining. Unknown/partial/partitioned snapshots fail closed until separately proven.

**Protocol:** GET `/api/cookie-sync/flow` must acknowledge `flow-session-v1` before capture for upload; POST to the same path must acknowledge that protocol and `accepted:true`. No fallback to `/api/cookie-sync`. HTTPS or loopback HTTP only, no redirects, no embedded URL credentials. The Flow opt-in is off by default, so existing users do not silently upload Google SSO credentials.

**Security:** Bundle contains only the tested fourteen `.google.com` auth cookies plus host-only `OSID` and `__Secure-OSID` at `flow.google.com`, all at `/`. Preserve cookie attributes. Capture one explicit cookie store and recheck the same tab/account and snapshot before sending. Labs cookie is not part of this protocol. Google-parent credentials can authenticate beyond Flow; popup explicitly warns users. Future server storage must encrypt and authorize this protocol before use outside the loopback test receiver.

- [x] Add failing tests for bundle selection, attributes, expiry, duplicate scopes, partitions and mixed stores.
- [x] Add failing tests for consent/disabled gates, identity changes, snapshot changes, destination changes, timeout, single-flight and receiver acknowledgement.
- [x] Implement pure contract and Chrome adapter; wire explicit opt-in and metadata-only status/check/sync actions.
- [x] Fix adjacent disabled/cookie-mode side effects so loading the collector cannot inject CAPTCHA scripts, reload tabs or send browser headers unexpectedly.
- [x] Run Node tests and syntax/diff checks; review security boundary a second time.
- [x] Load the actual extension into the original logged-in Cloak profile, using an isolated loopback receiver only. Prove disabled behavior, real capture, receiver body and replay into an independent clean target after restart. Never print raw credentials.
- [x] Record exact evidence and remaining limits. Disable the extension before closing the preserved source profile.

**Final evidence:** See `docs/flow-session-extension-v1.md`. Forty-five automated tests pass. The final content-addressed extension build passes twenty real source captures, one POST for three simultaneous sync calls, real popup messaging, clean-target restoration and restart. Source remains authenticated. An early browser run used a stale cached worker; that result was rejected, and final proof explicitly verifies current worker code. No new generation or remote backend changes are claimed.

**Rollback:** Revert only this extension patch after saving its diff; no schema/runtime migration. The new protocol is opt-in and rejects old receivers. Disable the Flow toggle to stop new collection. Existing legacy behavior remains available but is not claimed to support new Flow login.
