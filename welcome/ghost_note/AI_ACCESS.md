# GhostNote AI access

`GhostAccessToken.allow_ai` is independent of local and remote access. Migration
0020 sets it to false on all existing tokens. New real and test tokens default to
false. Only an owner/admin can enable it in the full token form, user token inline,
or owner API. AI-only tokens are valid. Neither purchase nor trial flows turn it on.

Old v1 routes and v2 leases retain their contracts. `/v2/lease/` defaults to integer
`license_version=2`; a new client requests integer 3 and signs the exact same proof
fields with the `GHOST-LEASE-REQUEST-V3` prefix. The signed V3 license has one extra
`0`/`1` field after local and remote permissions and before producer/viewer secrets.
V2 remains 15 lines plus its final newline; V3 is 16 lines plus its final newline.
An unsigned flag cannot change the requested protocol or grant AI access.

## HTTP contract

All AI endpoints are beneath `/ghost/v2/ai/`. Authentication uses
`X-Session-Id` and `X-Ghost-Lease` (producer secret, never the viewer key).
The server rechecks token dates, activation, AI permission, session revocation and
lease expiry independently of remote permission. Responses are `no-store`.

- `POST chat/`: `Content-Type: image/jpeg`, raw JPEG <=8 MiB,
  `X-Request-Id: canonical UUID`. Success returns `ok`, `answer`, `request_id`,
  `conversation_id` and `duplicate`.
- `GET history/`: returns `ok`, `conversation_id`, `messages`, `answer` (latest
  assistant response or empty) and `busy`. Message entries include role, content,
  ISO created_at and request_id; user entries mark `has_image` without exposing
  stored images. Empty history before the first request has an empty conversation_id.
- `POST reset/`: empty body and `X-Request-Id`; success returns `ok`,
  `conversation_id`, `duplicate`. Starts a new conversation without deleting old
  records. Duplicate reset UUIDs do not reset newly created conversation messages.

Errors are safe JSON `{ok:false,error:code}`. Permission denial is
`ai_not_allowed` (403); ordinary session/token errors are `invalid`, `not_started`,
`expired` (401). Busy threads return `ai_busy` (409), conflicting UUID/image pairs
return `ai_request_conflict` (409), and retrying a completed request from a prior
conversation returns `ai_conversation_reset` (409). Other codes include
`invalid_image` (400/413/415), `ai_timeout` (504), `ai_rate_limited` (429),
`ai_unavailable` / `ai_provider_error` (503), `ai_invalid_response` (502).

A transport failure or proxy error is ambiguous: retry the same image and UUID.
A parsed provider failure is terminal for its UUID: start a new request to try
again. A successful duplicate returns its stored answer without another charge.
No response exposes the provider API key or its raw failure body.

## Viewer text in AI mode

An active protected session with either `allow_ai` or `allow_remote` may open
`/ghost/v2/viewer/<session_id>/?key=<viewer_secret>&mode=text`, post human text to
`POST /ghost/v2/text/`, and consume it with `GET /ghost/v2/text/poll/`. The existing
JSON and authentication contracts stay unchanged: viewer secret for sending,
producer secret for polling. Token dates, activation, lease expiry and revocation
are checked on each request. Local-only tokens receive `text_not_allowed` (403).

AI-only tokens always render the focused text page, even without `mode=text`.
Remote-enabled tokens keep their full viewer unless they explicitly choose text
mode. AI-only owner links choose the protected text viewer while the lease is
active; otherwise the owner sees no legacy fallback link. These links contain
only the viewer credential, never the raw token or producer credential.

Every screenshot and audio endpoint still requires `allow_remote`; the focused
page requests neither. Human viewer text goes through the existing message queue,
is shown as plain text in the app, and does not become a Qwen prompt or alter AI
history. The latest received viewer message or AI answer is shown in the app.
Legacy v1 routes and behavior are unchanged.

## History and concurrency

History belongs to `(token, proven device public-key hash)`, not an ephemeral
producer session, and survives application restarts and lease renewals. Different
devices and tokens have separate conversations. Images and answers remain in the
server database; token deletion cascades to their history. The history response
contains up to the latest 30 turns of the current conversation, dropping oldest
response-only turns to keep serialized UTF-8 JSON at most 200 KiB (native limit:
256 KiB). The latest full answer and the complete database history are retained.

Provider context contains up to 20 previous turns, at most 24,000 assistant text
characters and at most 3 previous images. Previous images plus the current image
are capped at 12 MiB raw; older turns retain text and an image placeholder.
Database BLOBs are deferred and their lengths checked before reading images.
The provider owns the system prompt, provider key and HTTP transport.

There is one busy claim per conversation, with 120-second stale recovery. Network
I/O never runs inside a database transaction. New requests and resets while busy
return 409. Duplicate UUIDs are bound to the original image hash. After provider
completion, authorization and claim ownership are checked again: a disabled or
expired token, revoked/expired session, or stale worker cannot release its answer
or clear a newer claim. Failed and reset conversations remain stored for audit.

## Validation

`test_ai.py` covers signed V3 permission, unchanged V2 layout, migration defaults
for pre-existing real/test tokens, manual AI-only creation, old API payload
compatibility, revision conflicts, permission and device isolation, history,
context limits, retries, busy/reset, stale jobs and disable/revoke/expire during
model calls. Provider calls are mocked; tests never send customer images.

Run with an isolated database:

```
python -m django test ghost_note.test_ai ghost_note.test_security_v2 \
  ghost_note.test_owner_panel ghost_note.test_admin_token_inline \
  --settings=security_test_settings
```

The AI migration is additive. Deploy/migrate before serving the updated models.
Nginx must accept the 8 MiB image request and allow a provider response timeout.
Application keys must stay in server-only configuration, never in the EXE or PWA.
