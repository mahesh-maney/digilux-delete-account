# Digilux Honeywell — Account Deletion Feature
**Version:** 1.0
**Last updated:** 2025-08-16
**Owners:** Digilux Platform Team
**Solution Architect:** Nitin Saxena

---

## Table of Contents

1. [Feature Overview](#1-feature-overview)
2. [Architecture](#2-architecture)
3. [Data Inventory](#3-data-inventory)
4. [API Reference](#4-api-reference)
   - 4.1 [Phase 1 — DELETE /account (User-Facing)](#41-phase-1--delete-account-user-facing)
   - 4.2 [Phase 2 — POST /admin/archive (Admin Force-Archive)](#42-phase-2--post-adminarchive-admin-force-archive)
   - 4.3 [Complete Error Catalog](#43-complete-error-catalog)
5. [Integration Guide](#5-integration-guide)
6. [Audit Trail](#6-audit-trail)
7. [Monitoring & Alerting](#7-monitoring--alerting)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Feature Overview

### What it does

The Account Deletion feature allows a Digilux Honeywell user to permanently delete their account and all associated data. It is designed with two core principles:

- **Immediate revocation** — the user loses access the moment they confirm deletion. No grace period for re-entry.
- **Safe async cleanup** — actual data deletion happens out-of-band after an archive-first, verify-then-delete pipeline. Source data is never deleted without a confirmed backup.

### Why two phases?

A synchronous, single-request deletion (delete everything right now) carries unacceptable risk:

| Risk | Consequence |
|---|---|
| Lambda timeout mid-delete | Partial deletion with no record of what was removed |
| S3 archive fails silently | Source data deleted with no backup |
| DynamoDB throttle on one table | Other tables not cleaned up |

The two-phase design eliminates all of these:

- **Phase 1** completes in milliseconds (user-facing, returns immediately)
- **Phase 2** runs async, retries at the table level, and is gated by a hard archive verification step

### Key guarantees

1. A user can never log in after Phase 1 completes (Cognito user deleted, all sessions revoked).
2. All user data is archived to S3 before any source row is deleted.
3. If the S3 archive cannot be verified, hard-delete is **aborted** — data is preserved and an alert is raised.
4. Every step (success and failure) is recorded in a DynamoDB audit table and in structured CloudWatch log lines.
5. The pipeline is idempotent — calling Phase 1 on an already-INACTIVE user returns 200 without re-processing.

---

## 2. Architecture

### High-level flow

```
User App                  API Gateway              Lambda                 AWS Services
─────────                 ───────────              ──────                 ────────────
  │                            │                      │                        │
  │── DELETE /account ────────>│                      │                        │
  │   Bearer: <user JWT>       │── invoke ──────────>│                        │
  │                            │                      │── sign-out ───────────>│ Cognito
  │                            │                      │── mark INACTIVE ──────>│ DynamoDB (user_data)
  │                            │                      │── release devices ────>│ DynamoDB (device_data)
  │                            │                      │── delete Cognito user >│ Cognito
  │                            │                      │── write audit log ────>│ DynamoDB (deletion_audit)
  │<── 200 "processing" ───────│<── response ─────────│                        │
  │                            │                      │                        │
  │  (user cannot log in again from this point)       │                        │
  │                            │                      │                        │
  :  [ next day, 02:00 UTC ]   :                      :                        :
  :                            :                      :                        :
  :                        EventBridge ── invoke ────>│ (or admin POST)        :
  :                            :                      │── resolve all data ────>│ DynamoDB (14 tables)
  :                            :                      │── archive to S3 ───────>│ S3 (archive bucket)
  :                            :                      │── verify archive ───────>│ S3 head_object x14
  :                            :                      │── hard-delete source ──>│ DynamoDB + S3
  :                            :                      │── update audit log ────>│ DynamoDB (deletion_audit)
```

### Phase 1 — Immediate revocation (user-facing)

Triggered by the user. Must complete within the Lambda timeout (29 s API Gateway limit).

```
Step 1  Decode JWT          → extract sub claim as userId
Step 2  Idempotency check   → if status=INACTIVE, return 200 early (safe to re-call)
Step 3  Global sign-out     → admin_user_global_sign_out — all sessions revoked NOW
Step 4  Mark INACTIVE       → user_data: status=INACTIVE, archivePending=true  ← CRITICAL
Step 5  Release devices     → device_data: userId="0" (device unowned, not deleted)
Step 6  Delete Cognito user → admin_delete_user — login impossible forever
Step 7  Write audit log     → deletion_audit: PHASE1_COMPLETE + per-step outcomes
```

> **Step 4 is the only fatal step.** If it fails, the Lambda returns 500 and nothing else proceeds. All other step failures are logged and non-fatal — the pipeline continues.

### Phase 2 — Async archive and hard-delete

Triggered by EventBridge (daily at 02:00 UTC) OR by an admin POST call. Scans for all users with `status=INACTIVE AND archivePending=true`.

```
Step 1  Guard check         → confirm user is INACTIVE + archivePending=true
Step 2  Resolve             → collect every row from 14 DynamoDB tables + all S3 keys
Step 3  Archive             → write 14 JSON files to s3://digilux-honeywell-archive/archive/{userId}/dynamodb/
                             copy S3 metadata objects to s3://digilux-honeywell-archive/archive/{userId}/metadata/
Step 4  Verify              → head_object every archive file; abort if missing or empty  ← SAFETY GATE
Step 5  Hard-delete         → cascade tables first, then direct-userId tables, then S3
                             clear archivePending=false in user_data
                             update deletion_audit: PHASE2_COMPLETE / PHASE2_PARTIAL
```

> **Step 4 (Verify) is a hard gate.** `VerificationError` aborts the pipeline. Source data is never touched if the archive cannot be confirmed.

### EventBridge schedule

| Field | Value |
|---|---|
| Schedule expression | `cron(0 2 * * ? *)` |
| Runs | Every day at 02:00 UTC |
| Target | Phase 2 Lambda (`phase2_archive_worker`) |
| Event payload | `{ "source": "aws.scheduler" }` |

---

## 3. Data Inventory

### DynamoDB tables

| Table | What is stored | Deletion method |
|---|---|---|
| `digilux_honeywell_user_data` | User profile, status, preferences | `status` set to `INACTIVE`; `archivePending` cleared after Phase 2 |
| `digilux_honeywell_device_data` | Device registry per user | `userId` set to `"0"` in Phase 1 (unowned); rows deleted in Phase 2 |
| `digilux_honeywell_scene_data` | Scenes created by the user | Deleted in Phase 2 |
| `digilux_honeywell_user_device_details` | User-site-device binding details | Deleted in Phase 2 |
| `digilux_honeywell_user_device_mapping` | User-site device mapping | Deleted in Phase 2 |
| `digilux_honeywell_user_subuser_detail` | Sub-user relationships | Deleted in Phase 2 |
| `digilux_honeywell_user_subuser_mapping` | Sub-user invite/request records | Deleted in Phase 2 |
| `digilux_honeywell_subuser_role_data` | Role assignments for sub-users | Deleted in Phase 2 |
| `digilux_honeywell_admin_otp_data` | OTP records per module category | Deleted in Phase 2 |
| `digilux_honeywell_alexa_lwa_tokens` | Alexa LWA refresh tokens | Deleted in Phase 2 |
| `digilux_honeywell_device_state` | Last known state of each device | Deleted in Phase 2 |
| `digilux_honeywell_entity_state` | Endpoint-level entity state | Deleted in Phase 2 |
| `digilux_honeywell_automation_event` | Automation event definitions | Deleted in Phase 2 |
| `digilux_honeywell_automation_schedule_direct` | Direct automation schedules | Deleted in Phase 2 |
| `digilux_honeywell_automation_schedule_controller` | Controller automation schedules | Deleted in Phase 2 |
| `digilux_honeywell_deletion_audit` | Audit log — one record per user deletion | Written in Phase 1; updated in Phase 2 — **never deleted** |

### S3 buckets

| Bucket | Role |
|---|---|
| `digilux-honeywell-metadata` | Source — user's live metadata files (JSON per device/scene/site) |
| `digilux-honeywell-archive` | Destination — archived DynamoDB JSON + copied S3 metadata |

### Archive layout in S3

```
s3://digilux-honeywell-archive/
└── archive/
    └── {userId}/
        ├── dynamodb/
        │   ├── device_data.json
        │   ├── scene_data.json
        │   ├── user_device_details.json
        │   ├── user_device_mapping.json
        │   ├── user_subuser_detail.json
        │   ├── user_subuser_mapping.json
        │   ├── subuser_role_data.json
        │   ├── admin_otp_data.json
        │   ├── alexa_lwa_tokens.json
        │   ├── device_state.json
        │   ├── entity_state.json
        │   ├── automation_event.json
        │   ├── automation_schedule_direct.json
        │   └── automation_schedule_ctrl.json
        └── metadata/
            └── {userId}/
                └── ... (copied from digilux-honeywell-metadata)
```

### Cognito User Pool

| Field | Value |
|---|---|
| User Pool ID | `ap-south-1_KJpJMEzyM` |
| Region | `ap-south-1` |
| userId field | `sub` claim from the AccessToken JWT |
| Phase 1 actions | `admin_user_global_sign_out` then `admin_delete_user` |

---

## 4. API Reference

### Base URL

```
https://{api_id}.execute-api.ap-south-1.amazonaws.com/{stage}
```

Replace `{api_id}` with your API Gateway ID and `{stage}` with `prod`, `dev`, or `staging`.

### Authentication

Both endpoints use JWT Bearer tokens. The token is a Cognito **AccessToken**.

```
Authorization: Bearer <AccessToken>
```

The Lambda decodes the token locally (no Cognito API call) and extracts the `sub` claim as `userId`. **The token is not validated cryptographically inside the Lambda** — API Gateway's Cognito Authorizer must be configured to verify signatures before the request reaches the Lambda.

---

### 4.1 Phase 1 — DELETE /account (User-Facing)

Initiates account deletion for the authenticated user. Returns immediately — actual data removal happens async in Phase 2.

#### Endpoint

```
DELETE /account
```

#### Headers

| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <Cognito AccessToken>` |

#### Request body

None.

#### Responses

---

**200 OK — Deletion initiated**

The 7-step Phase 1 pipeline completed successfully. The user's Cognito account is deleted — they cannot log in again. Data will be cleaned up within 7 business days via Phase 2.

```json
{
    "message": "Your account deletion request has been received. Your access has been revoked immediately. All associated data will be permanently removed within 7 business days.",
    "status": "processing"
}
```

---

**200 OK — Already processed (idempotent)**

Phase 1 was already completed for this user (their record shows `status=INACTIVE`). Safe to call multiple times — returns the same acknowledgement without re-processing.

```json
{
    "message": "Your account deletion request has been received. Your access has been revoked immediately. All associated data will be permanently removed within 7 business days.",
    "status": "already_processed"
}
```

---

**401 Unauthorized — No token**

`Authorization` header is missing or empty.

```json
{
    "message": "Unauthorized",
    "error": "Authorization token missing"
}
```

---

**401 Unauthorized — Malformed JWT**

The token does not have 3 dot-separated segments.

```json
{
    "message": "Unauthorized",
    "error": "Invalid JWT format"
}
```

---

**401 Unauthorized — Missing sub claim**

The token is structurally valid but the payload does not contain a `sub` claim.

```json
{
    "message": "Unauthorized",
    "error": "Missing 'sub' in token claims"
}
```

---

**500 Internal Server Error — Mark INACTIVE failed**

DynamoDB `update_item` failed when trying to set `status=INACTIVE`. This is the only fatal failure in Phase 1 — all other step failures are non-fatal and logged but do not abort the pipeline.

```json
{
    "message": "Failed to process deletion request. Please try again."
}
```

---

#### Response headers (all responses)

```
Access-Control-Allow-Origin:  *
Access-Control-Allow-Methods: DELETE,OPTIONS
Content-Type:                 application/json
```

#### Example — cURL

```bash
curl -X DELETE \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/prod/account" \
  -H "Authorization: Bearer eyJraWQiOiJleGFtcGxlIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ1c2VyLXV1aWQtMTIzIn0.signature"
```

#### Status code summary

| Status | Meaning |
|---|---|
| `200` + `status: processing` | Success — pipeline complete |
| `200` + `status: already_processed` | Idempotent — already done |
| `401` | Auth failure — token missing, malformed, or no sub claim |
| `500` | Server error — only when mark-INACTIVE DynamoDB write fails |

---

### 4.2 Phase 2 — POST /admin/archive (Admin Force-Archive)

Archives and hard-deletes all data for a specific `userId`. Used by administrators to immediately process a user who is already INACTIVE, without waiting for the next EventBridge sweep (02:00 UTC).

> **This endpoint must be protected by an admin authorizer on API Gateway.** Regular users must not be able to call it.

#### Endpoint

```
POST /admin/archive
```

#### Headers

| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <Admin JWT>` |
| `Content-Type` | Yes | `application/json` |

#### Request body

```json
{
    "userId": "cognito-sub-uuid-abc-123"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `userId` | string | Yes | The `sub` claim value (UUID) of the user to archive. Must not be empty or whitespace-only. |

#### Prerequisite

Before calling this endpoint, the user **must** already be in state:
- `status = INACTIVE` in `digilux_honeywell_user_data`
- `archivePending = true` in `digilux_honeywell_user_data`

Both conditions are set by Phase 1. If they are not set, the endpoint returns 400.

#### Responses

---

**200 OK — Archive complete**

All 5 steps of Phase 2 completed successfully. All source data has been deleted.

```json
{
    "message": "Archive complete for userId=cognito-sub-uuid-abc-123"
}
```

---

**400 Bad Request — userId missing**

`userId` key is absent from the request body, or its value is an empty / whitespace-only string.

```json
{
    "error": "userId is required"
}
```

---

**400 Bad Request — userId not found or not eligible**

The `userId` does not exist in `user_data`, or the user is not `INACTIVE`, or `archivePending` is not `true`. These are all mapped to `ValueError` inside the archiver.

```json
{
    "error": "userId=cognito-sub-uuid-abc-123 not found in user_data"
}
```

```json
{
    "error": "userId=cognito-sub-uuid-abc-123 is not INACTIVE (status='ACTIVE')"
}
```

```json
{
    "error": "userId=cognito-sub-uuid-abc-123 archivePending is not True — already processed?"
}
```

---

**500 Internal Server Error — Archive verification failed**

The archive was written to S3 but `head_object` verification failed (file missing or empty). **Hard-delete was NOT performed.** Source data is preserved. Investigate the archive bucket before retrying.

```json
{
    "error": "Archive verification failed — hard-delete aborted. Archive file missing: archive/cognito-sub-uuid-abc-123/dynamodb/device_data.json"
}
```

---

**500 Internal Server Error — Unexpected error**

An unhandled exception occurred. Check CloudWatch logs for the full traceback.

```json
{
    "error": "An unexpected error description"
}
```

---

#### Response headers (all responses)

```
Access-Control-Allow-Origin:  *
Access-Control-Allow-Methods: POST,OPTIONS
Content-Type:                 application/json
```

#### Example — cURL

```bash
curl -X POST \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/prod/admin/archive" \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"userId": "cognito-sub-uuid-abc-123"}'
```

#### Status code summary

| Status | Meaning |
|---|---|
| `200` | Success — archive + hard-delete complete |
| `400` | Bad input — missing userId, or user not eligible |
| `500` | Server error — verification failed (data safe) or unexpected exception |

---

### 4.3 Complete Error Catalog

| HTTP Status | `error` / `message` value | Root cause | Safe to retry? |
|---|---|---|---|
| `401` | `Authorization token missing` | No `Authorization` header or empty Bearer value | Fix the request |
| `401` | `Invalid JWT format` | Token has fewer or more than 3 dot-separated segments | Fix the token |
| `401` | `Missing 'sub' in token claims` | Token payload does not contain `sub` claim | Fix the token |
| `400` | `userId is required` | Phase 2: `userId` absent or whitespace | Fix the request |
| `400` | `userId=... not found in user_data` | userId doesn't exist or Phase 1 never ran | Check userId / run Phase 1 first |
| `400` | `userId=... is not INACTIVE` | User's `status` is not `INACTIVE` | Run Phase 1 first |
| `400` | `archivePending is not True` | Phase 2 already completed for this user | No action needed |
| `500` | `Failed to process deletion request. Please try again.` | Phase 1: DynamoDB `update_item` failed (mark-INACTIVE step) | Retry Phase 1 |
| `500` | `Archive verification failed — hard-delete aborted.` | S3 archive file missing/empty after write | Investigate S3 + retry Phase 2 |
| `500` | *(any other message)* | Unexpected exception | Check CloudWatch logs |

---

## 5. Integration Guide

This section is for the **consuming team** integrating the Account Deletion feature into a client application (mobile, web, or backend service).

### Prerequisites

Before integration, confirm the following with the Digilux Platform Team:

- [ ] Your API Gateway endpoint URL (`api_id` + `stage`)
- [ ] Your Cognito App Client ID (`cognito_client_id`)
- [ ] Cognito User Pool ID (`ap-south-1_KJpJMEzyM`)
- [ ] Admin JWT or API key for the Phase 2 force-archive endpoint (if your service needs it)
- [ ] Your app's IAM or API Gateway authorizer is configured (if calling the admin endpoint)

---

### Sequential call order

The following is the **only correct call sequence**. Do not deviate from this order.

```
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 1 — Authenticate the user (your existing auth flow)           │
│                                                                     │
│  POST https://cognito-idp.ap-south-1.amazonaws.com/                │
│  Target: AWSCognitoIdentityProviderService.InitiateAuth             │
│  Body: { AuthFlow, ClientId, AuthParameters: { USERNAME, PASSWORD }}│
│                                                                     │
│  Save: AuthenticationResult.AccessToken → use as Bearer token       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 2 — Present deletion confirmation screen to the user          │
│                                                                     │
│  Show the user a clear warning:                                     │
│  "This is permanent. Your account and all data will be deleted.     │
│   You will be signed out immediately."                              │
│                                                                     │
│  Require explicit confirmation (e.g., type "DELETE" or tap button). │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 3 — Call Phase 1 (DELETE /account)                            │
│                                                                     │
│  DELETE /account                                                    │
│  Authorization: Bearer <AccessToken from Step 1>                    │
│                                                                     │
│  Expected response: 200 { status: "processing" }                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 4 — Handle the response in your app                           │
│                                                                     │
│  On 200:                                                            │
│    • Clear all local session data (tokens, cache, user preferences) │
│    • Navigate to a "Your account has been deleted" screen           │
│    • Do NOT attempt to refresh the token or call any other API      │
│                                                                     │
│  On 401:                                                            │
│    • Token has expired or is invalid                                │
│    • Re-authenticate (Step 1) and retry Step 3 once                 │
│                                                                     │
│  On 500:                                                            │
│    • Show a generic error: "Unable to process your request.         │
│      Please try again."                                             │
│    • Retry is safe — Phase 1 is idempotent                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 5 — Data is cleaned up automatically (no action needed)       │
│                                                                     │
│  EventBridge runs at 02:00 UTC daily.                               │
│  Phase 2 picks up this user on the next sweep and permanently       │
│  removes all data within 24 hours.                                  │
│                                                                     │
│  (Admin teams can force-archive immediately — see Section 5.3)      │
└─────────────────────────────────────────────────────────────────────┘
```

---

### 5.1 Step-by-step: Obtaining a Cognito AccessToken

If you are integrating from a backend service (not a mobile app), use the `InitiateAuth` API directly:

**Request:**

```bash
curl -X POST "https://cognito-idp.ap-south-1.amazonaws.com/" \
  -H "X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth" \
  -H "Content-Type: application/x-amz-json-1.1" \
  -d '{
    "AuthFlow": "USER_PASSWORD_AUTH",
    "ClientId": "YOUR_COGNITO_CLIENT_ID",
    "AuthParameters": {
      "USERNAME": "user@example.com",
      "PASSWORD": "UserPassword123!"
    }
  }'
```

**Response:**

```json
{
    "AuthenticationResult": {
        "AccessToken": "eyJraWQiOiJleGFtcGxlIiwidHlwIjoiSldUIn0...",
        "ExpiresIn": 3600,
        "TokenType": "Bearer",
        "RefreshToken": "eyJjdHkiOiJKV1QiLCJlbmMiOiJBMjU2R0NNIiwiYWxnIjoiUlNBLU9BRVAifQ...",
        "IdToken": "eyJraWQiOiJleGFtcGxlIiwidHlwIjoiSldUIn0..."
    },
    "ChallengeParameters": {}
}
```

Use `AuthenticationResult.AccessToken` as the Bearer token for `DELETE /account`.

> **Token expiry:** AccessTokens expire after 3600 seconds (1 hour). If you receive a 401 on Phase 1, your token may have expired. Re-authenticate and retry.

---

### 5.2 Step-by-step: Calling Phase 1

**Request:**

```bash
curl -X DELETE \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/prod/account" \
  -H "Authorization: Bearer eyJraWQiOiJleGFtcGxlIiwidHlwIjoiSldUIn0..."
```

**Success response (200):**

```json
{
    "message": "Your account deletion request has been received. Your access has been revoked immediately. All associated data will be permanently removed within 7 business days.",
    "status": "processing"
}
```

**What your app must do after a 200 response:**

```
1. Clear all stored tokens (AccessToken, RefreshToken, IdToken)
2. Clear any locally cached user data
3. Invalidate any in-memory session state
4. Redirect the user to a post-deletion screen
5. Do NOT call any other Digilux API with this user's credentials
```

**Idempotency:** If your app calls `DELETE /account` again (e.g., due to a network retry), and Phase 1 has already completed, you will receive:

```json
{
    "message": "Your account deletion request has been received. ...",
    "status": "already_processed"
}
```

This is a 200 and is safe. Treat it identically to `status: processing`.

---

### 5.3 Step-by-step: Admin Force-Archive (Phase 2)

This is only needed if your admin tooling needs to immediately archive a user rather than waiting for the next daily sweep. Most consuming teams will never need to call this directly.

**Prerequisite check:** Before calling, confirm the user is INACTIVE:

```bash
# Check user_data table (requires DynamoDB access)
aws dynamodb get-item \
  --table-name digilux_honeywell_user_data \
  --key '{"userId": {"S": "cognito-sub-uuid-abc-123"}}'
```

Confirm the response shows `"status": {"S": "INACTIVE"}` and `"archivePending": {"BOOL": true}`.

**Request:**

```bash
curl -X POST \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/prod/admin/archive" \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"userId": "cognito-sub-uuid-abc-123"}'
```

**Success response (200):**

```json
{
    "message": "Archive complete for userId=cognito-sub-uuid-abc-123"
}
```

**On 500 with verification failure:**

```json
{
    "error": "Archive verification failed — hard-delete aborted. Archive file missing: archive/cognito-sub-uuid-abc-123/dynamodb/device_data.json"
}
```

Action: Check `s3://digilux-honeywell-archive/archive/{userId}/dynamodb/` for partial writes. Investigate S3 bucket permissions and retry.

---

### 5.4 Retry policy

| Scenario | Safe to retry? | Recommended action |
|---|---|---|
| Phase 1 → 401 | Yes | Re-authenticate and retry once |
| Phase 1 → 500 (`"Failed to process deletion request"`) | Yes | Retry up to 3 times with exponential back-off |
| Phase 1 → 200 (any status) | No retry needed | Done |
| Phase 2 → 400 (userId not found or not eligible) | No | Fix the precondition first |
| Phase 2 → 500 (verification failed) | Yes | Investigate S3, retry after fix |
| Phase 2 → 500 (unexpected error) | Yes | Retry once; if it persists, escalate to platform team |

---

### 5.5 What NOT to do

| Do NOT | Why |
|---|---|
| Call `DELETE /account` without user confirmation | Irreversible. Cannot be undone. |
| Attempt to refresh the token after Phase 1 succeeds | The Cognito user is deleted. The refresh token is invalid. |
| Call any other Digilux API with the deleted user's credentials | They will fail with 401 from Cognito. |
| Call Phase 2 before Phase 1 | The user is not INACTIVE yet — Phase 2 will return 400. |
| Hard-code the `userId` | Always extract it from the `sub` claim of the JWT — never from a username or email. |
| Ignore a 500 on Phase 1 | The user may still be active. Show an error and allow them to retry. |
| Poll Phase 2 to check if data is deleted | Data removal is async. It happens within 24 hours automatically. There is no "done" webhook. |

---

## 6. Audit Trail

Every deletion is fully auditable. A record is written to `digilux_honeywell_deletion_audit` and structured log lines are emitted to CloudWatch.

### 6.1 DynamoDB audit record schema

**Key schema:** `userId` (PK, String) + `requestedAt` (SK, String ISO-8601 timestamp)

**After Phase 1 completes:**

```json
{
    "userId":               "cognito-sub-uuid-abc-123",
    "requestedAt":          "2025-08-16T02:14:37.123456+00:00",
    "status":               "PHASE1_COMPLETE",
    "phase1CompletedAt":    "2025-08-16T02:14:37.123456+00:00",
    "globalSignOutStatus":  "ok",
    "devicesFound":         3,
    "devicesReleased":      3,
    "devicesReleaseFailed": 0,
    "cognitoDeleteStatus":  "ok",
    "deletedBy":            "cognito-sub-uuid-abc-123"
}
```

**After Phase 2 completes (fields added):**

```json
{
    "userId":               "cognito-sub-uuid-abc-123",
    "requestedAt":          "2025-08-16T02:14:37.123456+00:00",
    "status":               "PHASE2_COMPLETE",
    "phase1CompletedAt":    "2025-08-16T02:14:37.123456+00:00",
    "globalSignOutStatus":  "ok",
    "devicesFound":         3,
    "devicesReleased":      3,
    "devicesReleaseFailed": 0,
    "cognitoDeleteStatus":  "ok",
    "deletedBy":            "cognito-sub-uuid-abc-123",
    "resolveCompletedAt":   "2025-08-17T02:00:04.221000+00:00",
    "archiveCompletedAt":   "2025-08-17T02:00:07.832000+00:00",
    "verifyCompletedAt":    "2025-08-17T02:00:08.105000+00:00",
    "hardDeleteCompletedAt":"2025-08-17T02:00:09.441000+00:00",
    "phase2CompletedAt":    "2025-08-17T02:00:09.442000+00:00",
    "tablesDeleted": {
        "automation_event":              2,
        "automation_schedule_direct":    1,
        "automation_schedule_ctrl":      1,
        "device_state":                  3,
        "entity_state":                  6,
        "scene_data":                    4,
        "device_data":                   3,
        "user_device_details":           1,
        "user_device_mapping":           1,
        "user_subuser_detail":           0,
        "user_subuser_mapping":          0,
        "subuser_role_data":             1,
        "admin_otp_data":                2,
        "alexa_lwa_tokens":              1
    },
    "s3ObjectsDeleted":     12,
    "errors":               []
}
```

**Possible `status` values:**

| Value | Meaning |
|---|---|
| `PHASE1_COMPLETE` | Phase 1 done; Phase 2 not yet run |
| `PHASE2_COMPLETE` | Full pipeline complete; all data removed |
| `PHASE2_PARTIAL` | Phase 2 ran but some individual row deletes failed (errors list will be non-empty) |

**`globalSignOutStatus` values:**

| Value | Meaning |
|---|---|
| `ok` | Sign-out succeeded |
| `user_not_found` | Cognito user was not found — may already be deleted |
| `error` | Sign-out threw an unexpected exception |
| `not_attempted` | Step was skipped (should not occur in normal flow) |

**`cognitoDeleteStatus` values:**

| Value | Meaning |
|---|---|
| `ok` | Cognito user deleted |
| `already_absent` | Cognito user did not exist (sign-out may have deleted them, or they were never in Cognito) |
| `error` | Delete threw an unexpected exception |

---

### 6.2 CloudWatch audit events

Every significant action emits a structured JSON log line. Use CloudWatch Logs Insights to query them.

**Log group:** The Lambda function's log group (e.g., `/aws/lambda/phase1_account_deletion` and `/aws/lambda/phase2_archive_worker`).

**Event types emitted:**

| `audit_event` | Phase | Key fields |
|---|---|---|
| `DELETION_REQUEST_RECEIVED` | 1 | `userId`, `trigger: "self"` |
| `DELETION_ALREADY_PROCESSED` | 1 | `userId` |
| `GLOBAL_SIGN_OUT` | 1 | `userId`, `status` |
| `USER_MARKED_INACTIVE` | 1 | `userId`, `archivePending`, `markedAt` |
| `MARK_INACTIVE_FAILED` | 1 | `userId` |
| `DEVICES_RELEASED` | 1 | `userId`, `devicesFound`, `devicesReleased`, `devicesReleaseFailed` |
| `DEVICES_RELEASE_FAILED` | 1 | `userId`, `reason` |
| `COGNITO_USER_DELETED` | 1 | `userId`, `status` |
| `PHASE1_COMPLETE` | 1 | `userId` + all step outcomes |
| `PHASE2_STARTED` | 2 | `userId` |
| `RESOLVE_COMPLETE` | 2 | `userId`, `devices`, `scenes`, `automationEvents`, `s3Objects` |
| `ARCHIVE_COMPLETE` | 2 | `userId`, `dynamodbCollections: 14`, `s3ObjectsCopied` |
| `VERIFY_COMPLETE` | 2 | `userId`, `filesVerified: 14` |
| `HARD_DELETE_COMPLETE` | 2 | `userId`, `tablesDeleted`, `s3ObjectsDeleted` |
| `HARD_DELETE_PARTIAL` | 2 | `userId`, `errors[]`, `tablesDeleted` |
| `PHASE2_COMPLETE` | 2 | `userId` |

**Example log line (raw):**

```json
{
    "audit_event": "PHASE1_COMPLETE",
    "userId": "cognito-sub-uuid-abc-123",
    "timestamp": "2025-08-16T02:14:37.123456+00:00",
    "globalSignOutStatus": "ok",
    "devicesFound": 3,
    "devicesReleased": 3,
    "devicesReleaseFailed": 0,
    "cognitoDeleteStatus": "ok"
}
```

---

## 7. Monitoring & Alerting

### Recommended CloudWatch Logs Insights queries

**Trace the full deletion lifecycle for one user:**

```
fields @timestamp, audit_event, status, devicesFound, devicesReleased, tablesDeleted, errors
| filter userId = "cognito-sub-uuid-abc-123"
| sort @timestamp asc
```

**All Phase 1 completions in the last 24 hours:**

```
filter audit_event = "PHASE1_COMPLETE"
| stats count() as total by bin(1h)
```

**All verification failures (data was NOT deleted — needs attention):**

```
filter audit_event = "HARD_DELETE_PARTIAL" or message like "verification failed"
| fields @timestamp, userId, errors
| sort @timestamp desc
```

**Devices that failed to release during Phase 1:**

```
filter audit_event = "DEVICES_RELEASED" and devicesReleaseFailed > 0
| fields @timestamp, userId, devicesFound, devicesReleased, devicesReleaseFailed
```

**Phase 2 sweep summary (how many users processed per sweep):**

```
filter message like "Sweep complete"
| fields @timestamp, message
| sort @timestamp desc
```

### Recommended CloudWatch Alarms

| Alarm | Metric / Pattern | Threshold | Action |
|---|---|---|---|
| Verification failures | `filter message like "verification failed"` | Count > 0 in 5 min | Page on-call |
| Phase 1 DynamoDB failure | `filter audit_event = "MARK_INACTIVE_FAILED"` | Count > 0 in 5 min | Alert platform team |
| Phase 2 sweep zero-processed | `filter message like "Sweep complete" and processed = 0` | Check if pending users exist | Investigate EventBridge |
| High device release failures | `filter audit_event = "DEVICES_RELEASED" and devicesReleaseFailed > 0` | Count > 5 in 1 hour | Alert device team |
| Lambda errors | `Lambda Errors` metric | Count > 2 in 5 min | Alert platform team |

---

## 8. Troubleshooting

### Phase 1 issues

---

**Problem:** User receives 401 even with a valid-looking token.

**Cause:** Token may have expired (Cognito AccessTokens expire after 1 hour) or the API Gateway Cognito Authorizer is rejecting it.

**Fix:**
1. Decode the token at [jwt.io](https://jwt.io) and check the `exp` claim.
2. Re-authenticate to get a fresh token.
3. If the issue persists, check API Gateway Authorizer logs.

---

**Problem:** User receives 500 "Failed to process deletion request."

**Cause:** DynamoDB `update_item` failed when trying to set `status=INACTIVE`. Possible causes: DynamoDB throttling, table does not exist, Lambda IAM role missing `dynamodb:UpdateItem` permission.

**Fix:**
1. Check CloudWatch logs for the `[{userId}] Failed to mark INACTIVE` error line.
2. Verify the Lambda execution role has `dynamodb:UpdateItem` on `digilux_honeywell_user_data`.
3. Check DynamoDB CloudWatch metrics for throttling.
4. Retry — Phase 1 is idempotent.

---

**Problem:** User says they can still log in after deletion.

**Cause:** Phase 3 global sign-out or Cognito delete failed silently. Check the audit record.

**Fix:**
1. Query `deletion_audit` for this user. Check `globalSignOutStatus` and `cognitoDeleteStatus`.
2. If either is `error`, manually call `admin_user_global_sign_out` and `admin_delete_user` via the AWS CLI:

```bash
aws cognito-idp admin-user-global-sign-out \
  --user-pool-id ap-south-1_KJpJMEzyM \
  --username cognito-sub-uuid-abc-123

aws cognito-idp admin-delete-user \
  --user-pool-id ap-south-1_KJpJMEzyM \
  --username cognito-sub-uuid-abc-123
```

---

### Phase 2 issues

---

**Problem:** `PHASE2_PARTIAL` in audit log — `errors` list is non-empty.

**Cause:** One or more individual `delete_item` calls failed (throttling, permission issue, item already deleted).

**Fix:**
1. Check `tablesDeleted` in the audit record — the count shows which tables had failures.
2. Rows that failed to delete remain in the source table. Re-run Phase 2 (call the force-archive endpoint again).
3. The guard check (`archivePending=true`) is cleared after Phase 2 runs — you may need to reset it manually before re-running:

```bash
aws dynamodb update-item \
  --table-name digilux_honeywell_user_data \
  --key '{"userId": {"S": "cognito-sub-uuid-abc-123"}}' \
  --update-expression "SET archivePending = :t" \
  --expression-attribute-values '{":t": {"BOOL": true}}'
```

---

**Problem:** Phase 2 returns `500 verification failed — hard-delete aborted`.

**Cause:** Archive write appeared to succeed but `head_object` could not confirm the file (eventual consistency, S3 permissions, or write failure).

**Fix:**
1. Check `s3://digilux-honeywell-archive/archive/{userId}/dynamodb/` for the named missing file.
2. Verify the Lambda execution role has `s3:HeadObject` on `digilux-honeywell-archive`.
3. If the file is there but reported missing, wait 10–30 seconds (S3 eventual consistency) and retry.
4. Source data is **not touched** — it is safe to retry.

---

**Problem:** Phase 2 sweep ran but a user with `status=INACTIVE` and `archivePending=true` was not processed.

**Cause:** DynamoDB scan may have missed the user due to an item that fails the FilterExpression (e.g., `archivePending` stored as a string `"true"` instead of a boolean `true`).

**Fix:**
1. Inspect the user's record:

```bash
aws dynamodb get-item \
  --table-name digilux_honeywell_user_data \
  --key '{"userId": {"S": "cognito-sub-uuid-abc-123"}}'
```

2. Confirm `archivePending` is `{"BOOL": true}` (not `{"S": "true"}`).
3. Use the force-archive endpoint to immediately process this user.

---

**Problem:** The same `automation_event` row is archived twice.

**Cause:** This was a known edge case where a row is reachable via both `sceneId` and `duid` GSIs. The archiver deduplicates by `automationId` internally.

**Resolution:** Already handled — deduplication is built into `_resolve_automations`. If you see duplicates in the archive file, it is a pre-existing data issue in the source table, not an archiver bug.

---

### Checklist: confirming a clean full deletion

To verify that a user's deletion is fully complete:

```
[ ] deletion_audit record has status = PHASE2_COMPLETE
[ ] deletion_audit.errors is an empty list []
[ ] deletion_audit.tablesDeleted counts match expected row counts
[ ] user_data.status = INACTIVE and user_data.archivePending = false
[ ] user_data.archivedAt is set
[ ] s3://digilux-honeywell-archive/archive/{userId}/dynamodb/ contains all 14 JSON files
[ ] Cognito: admin-get-user returns UserNotFoundException
[ ] device_data: no rows with this userId (userId = "0" from Phase 1 release)
[ ] CloudWatch: PHASE2_COMPLETE event visible in logs
```

---

*For questions or escalations, contact the Digilux Platform Team.*
