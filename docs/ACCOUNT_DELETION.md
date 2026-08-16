# Digilux Honeywell — Account Deletion Feature

**Version:** 2.0
**Last updated:** 2026-08-16
**Owner:** Digilux Platform Team

---

## Table of Contents

1. [Feature Overview](#1-feature-overview)
2. [Architecture](#2-architecture)
3. [Data Inventory](#3-data-inventory)
4. [API Reference](#4-api-reference)
5. [Integration Guide](#5-integration-guide)
6. [Audit Trail](#6-audit-trail)
7. [Monitoring & Alerting](#7-monitoring--alerting)
8. [Troubleshooting](#8-troubleshooting)
9. [Deployment Guide (DevOps Handover)](#9-deployment-guide-devops-handover)

---

## 1. Feature Overview

### What it does

A Digilux Honeywell user can permanently delete their account and all associated data. The system:

- Revokes access **immediately** the moment the user confirms deletion
- Gives a **7-day restore window** — the user can cancel and get everything back
- Returns a **deletion token** the user can use to track or dispute the deletion
- Archives all data to S3 **before** deleting any source record — hard-delete never runs without a verified archive
- Permanently removes all data after the 7-day window closes

### Why two phases?

Deleting everything in one synchronous request is unsafe — a timeout or partial failure can leave data in an unrecoverable state. The two-phase design separates the instant user-facing action from the heavier async cleanup.

| Phase | When | What happens |
|---|---|---|
| Phase 1 | Immediately, on user request | Sessions revoked, account disabled, devices unbound |
| Phase 2 | After 7-day window (auto via EventBridge or admin-triggered) | Archive to S3 → verify → hard-delete all data + Cognito user |

### Key guarantees

1. User **cannot log in** the moment Phase 1 completes (all sessions invalidated, Cognito account disabled).
2. User has **7 days to restore** their account using the deletion token.
3. **All data is archived to S3** before any source row is deleted. Verification is a hard gate — if it fails, hard-delete is aborted.
4. The **deletion token** returned at Phase 1 proves when and from where the request was made — useful for disputes.
5. Every step is recorded in DynamoDB and CloudWatch with structured JSON — queryable per user.

---

## 2. Architecture

### High-level flow

```
User App               API Gateway          Lambda                AWS Services
--------               -----------          ------                ------------
|                           |                   |                      |
|-- DELETE /account ------->|-- invoke --------->|-- revoke sessions -->| Cognito
|                           |                   |-- disable account -->| Cognito
|                           |                   |-- unbind devices --->| DynamoDB
|                           |                   |-- write evidence --->| DynamoDB
|<-- 200 {deletionToken} ---|<-- response --------|                      |
|                           |                   |                      |
|  [within 7 days - user can restore]           |                      |
|-- POST /account/restore -->|-- invoke --------->|-- re-enable login -->| Cognito
|                           |                   |-- restore tables --->| DynamoDB
|<-- 200 "restored" --------|<-- response --------|                      |
|                           |                   |                      |
|  [after 7 days - EventBridge daily at 02:00 UTC, or admin POST]      |
|                     EventBridge -- invoke --->|-- 7-day gate check  |
|                           |                   |-- read 15 tables --->| DynamoDB
|                           |                   |-- scan S3 endpoints->| S3 (metadata)
|                           |                   |-- archive to S3 ---->| S3 (archive)
|                           |                   |-- verify archive --->| S3
|                           |                   |-- hard-delete ------>| DynamoDB + S3
|                           |                   |-- delete Cognito --->| Cognito
|                           |                   |-- update audit ----->| DynamoDB
```

### Phase 1 steps (user-facing, must complete within 29 s)

```
Step 1   Decode JWT               Extract sub claim -> userId
Step 2   Idempotency check        Already INACTIVE? Return 200 early - safe to re-call
Step 3   Global sign-out          admin_user_global_sign_out - all sessions revoked now
Step 4   Mark INACTIVE            user_data: status=INACTIVE, archivePending=true  <- only fatal step
Step 5   Unbind devices           device_data: userId="0" for all owned devices
Step 6   Disable Cognito          admin_disable_user - login blocked, user still exists for restore
Step 7   Write deletion evidence  deletion_evidence: userId, emailEncrypted, ipAddress, deletionToken
Step 8   Write audit log          deletion_audit: PHASE1_COMPLETE + all step outcomes
         Return 200               { status, deletionToken }
```

> Step 4 is the only fatal step. If it fails, Lambda returns 500 and nothing else runs. All other step failures are logged as non-fatal and the pipeline continues.

### Phase 2 steps (async, triggered by EventBridge daily at 02:00 UTC)

```
Step 1   Guard check              Confirm status=INACTIVE + archivePending=true
Step 2   7-day gate               Reject if now - deletionRequestedAt < 7 days
Step 3   Resolve                  Collect all rows from 15 DynamoDB tables + user S3 keys
Step 4   Scan S3 endpoints        List all endpoints.json under {userId}/ in metadata bucket
                                  Skip deviceType=11 (gateway - keep in system)
                                  Collect unique subdeviceMacAddress values from remaining entries
                                  Query device_data via macAddress-index GSI -> rows to delete
Step 5   Archive                  Write 15 JSON files to archive/{userId}/dynamodb/
                                  Copy S3 metadata objects to archive/{userId}/metadata/
Step 6   Verify                   head_object every archive file - abort if missing or empty  <- hard gate
Step 7   Hard-delete              Delete all resolved DynamoDB rows
                                  Delete subdevice rows from device_data
                                  Delete source S3 metadata objects
                                  Set user_data.archivePending=false
Step 8   Delete Cognito user      admin_delete_user - permanent, login impossible forever
Step 9   Update audit log         deletion_audit: PHASE2_COMPLETE / PHASE2_PARTIAL
         Update evidence          deletion_evidence: archiveDeletedAt = now
```

> Step 6 is a hard gate. If verification fails, steps 7-9 do not run. Source data is preserved and an alert fires.

### Restore flow (within 7-day window only)

```
Step 1   Validate token        Look up deletion_evidence by deletionToken
Step 2   Check window          Reject if now - requestedAt > 7 days
Step 3   Re-enable Cognito     admin_enable_user - login re-allowed
Step 4   Restore DynamoDB      Read 15 JSON archives from S3, PUT rows back to source tables
Step 5   Re-bind devices       device_data: set userId back from "0" to original userId
Step 6   Clear flags           user_data: status=ACTIVE, archivePending=false
Step 7   Update records        deletion_evidence: restoredAt=now; deletion_audit: RESTORED
         Return 200            { message: "Account restored" }
```

### EventBridge schedule

| Setting | Value |
|---|---|
| Schedule | `cron(0 2 * * ? *)` - every day at 02:00 UTC |
| Target | Phase 2 Lambda |
| Retry | Max 2 attempts |

---

## 3. Data Inventory

### DynamoDB tables

| Table | What is stored | Action on deletion |
|---|---|---|
| `digilux_honeywell_user_data` | User profile, status, preferences | Phase 1: marked INACTIVE. Phase 2: archivePending cleared. Never deleted. |
| `digilux_honeywell_device_data` | Device registry | Phase 1: userId set to "0" (unbound). Phase 2: gateway rows kept; subdevice rows (deviceType=12) hard-deleted via S3 endpoint scan. |
| `digilux_honeywell_scene_data` | Scenes created by user | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_user_device_details` | User-site-device binding details | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_user_device_mapping` | User-site device mapping | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_user_subuser_detail` | Sub-user relationships | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_user_subuser_mapping` | Sub-user invite records | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_subuser_role_data` | Role assignments for sub-users | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_admin_otp_data` | OTP records | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_alexa_lwa_tokens` | Alexa LWA refresh tokens | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_device_state` | Last known device state | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_entity_state` | Endpoint-level entity state | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_automation_event` | Automation event definitions | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_automation_schedule_direct` | Direct device automation schedules | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_automation_schedule_controller` | Network controller automation schedules | Archive + hard-delete in Phase 2 |
| `digilux_honeywell_deletion_audit` | Pipeline audit log - one record per user deletion | Written Phase 1, updated Phase 2. **Never deleted.** |
| `digilux_honeywell_deletion_evidence` | Compliance record - encrypted email, IP, token, timestamps | Written Phase 1. **Never deleted.** |

### device_data: gateway vs subdevice

The same table holds two types of rows, handled differently:

| Row type | deviceType | Treatment |
|---|---|---|
| Gateway / WiFiBridge | `11` | Unbind only (userId -> "0"). Hardware stays in the system. |
| Zigbee subdevice (lights, drivers, sensors) | `12` | Hard-deleted in Phase 2 via S3 endpoint scan. |

**How Phase 2 discovers subdevice rows:**

```
S3 path scanned:
  s3://{metadata_bucket}/{userId}/site_{siteId}/devices/{deviceId}/{gatewayMac}/{subdeviceMac}/endpoints.json

For each file found:
  1. Parse the JSON array of endpoint objects
  2. Skip entries where deviceType == 11 (gateway)
  3. Collect unique subdeviceMacAddress values from remaining entries
  4. Query device_data using macAddress-index GSI for each MAC
  5. Hard-delete matched rows by (deviceId, macAddress)
```

### S3 buckets

| Bucket | Role |
|---|---|
| `digilux-honeywell-metadata` | Live user metadata - endpoint files, scene files, zone files |
| `digilux-honeywell-archive` | Deletion archive - DynamoDB JSON exports + copied metadata |

**Archive layout:**

```
s3://digilux-honeywell-archive/archive/{userId}/
+-- dynamodb/
|   +-- scene_data.json
|   +-- device_data.json
|   +-- user_device_details.json
|   +-- user_device_mapping.json
|   +-- user_subuser_detail.json
|   +-- user_subuser_mapping.json
|   +-- subuser_role_data.json
|   +-- admin_otp_data.json
|   +-- alexa_lwa_tokens.json
|   +-- device_state.json
|   +-- entity_state.json
|   +-- automation_event.json
|   +-- automation_schedule_direct.json
|   +-- automation_schedule_controller.json
|   +-- automation_schedule_ctrl.json
+-- metadata/
    +-- {userId}/  (copied from digilux-honeywell-metadata)
```

### Cognito

| Setting | Value |
|---|---|
| User Pool ID | `ap-south-1_KJpJMEzyM` |
| userId | `sub` claim from the Cognito AccessToken |
| Phase 1 | `admin_user_global_sign_out` then `admin_disable_user` |
| Phase 2 | `admin_delete_user` (after archive verified, after 7-day window) |
| Restore | `admin_enable_user` (within 7-day window only) |

---

## 4. API Reference

### Base URL

```
https://{api_id}.execute-api.ap-south-1.amazonaws.com/{stage}
```

### Authentication

All endpoints use Cognito AccessToken JWTs as Bearer tokens. The `sub` claim is the `userId`.

```
Authorization: Bearer <AccessToken>
```

---

### 4.1 DELETE /account - Initiate Deletion (Phase 1)

Initiates account deletion for the authenticated user. Returns immediately. Data is not removed yet.

**Request**

```
DELETE /account
Authorization: Bearer <AccessToken>
```

No request body.

**200 OK - Success**

```json
{
    "message": "Your account deletion request has been received. Your access has been revoked immediately. All associated data will be permanently removed within 7 business days.",
    "status": "processing",
    "deletionToken": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

Save `deletionToken` - the user needs it to check status or request a restore within 7 days.

**200 OK - Already processed (idempotent)**

```json
{
    "message": "...",
    "status": "already_processed"
}
```

**401 Unauthorized** - Token missing, malformed, or no `sub` claim.

**500 Internal Server Error** - Mark-INACTIVE DynamoDB step failed. Safe to retry.

---

### 4.2 POST /account/restore - Restore Account

Cancels a pending deletion and fully restores the account. Only valid within 7 days of Phase 1.

**Request**

```
POST /account/restore
Content-Type: application/json
```

```json
{
    "token": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

**200 OK - Restored**

```json
{
    "message": "Your account has been restored. You can log in again.",
    "userId": "cognito-sub-uuid-abc-123",
    "restoredAt": "2026-08-20T10:30:00.000000+00:00"
}
```

**400 Bad Request** - Token not found, or 7-day window has expired.

```json
{ "error": "Restore window has expired. Data was permanently deleted on 2026-08-23T02:14:37+00:00." }
```

```json
{ "error": "Invalid or unknown deletion token." }
```

**409 Conflict** - Account was already restored.

```json
{ "error": "Account already restored." }
```

---

### 4.3 GET /account/deletion-status - Check Deletion Timeline

Returns the full deletion timeline for a given token. No Bearer token required - the deletion token is the credential. Used when a user claims they did not initiate the deletion.

**Request**

```
GET /account/deletion-status?token=f47ac10b-58cc-4372-a567-0e02b2c3d479
```

**200 OK**

```json
{
    "userId": "cognito-sub-uuid-abc-123",
    "requestedAt": "2026-08-16T02:14:37+00:00",
    "ipAddress": "203.0.113.42",
    "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 ...)",
    "archiveStartedAt": "2026-08-23T02:00:04+00:00",
    "archiveCompletedAt": "2026-08-23T02:00:09+00:00",
    "archiveDeletedAt": null,
    "restoredAt": null,
    "status": "PHASE2_COMPLETE"
}
```

| `status` | Meaning |
|---|---|
| `PHASE1_COMPLETE` | Deletion requested, restore still possible |
| `PHASE2_COMPLETE` | All data permanently deleted |
| `RESTORED` | Account was restored within the 7-day window |

**400 Bad Request** - Token missing or not found.

```json
{ "error": "Invalid or unknown deletion token." }
```

---

### 4.4 POST /admin/archive - Force Archive (Admin Only)

Immediately runs Phase 2 for a specific user. Bypasses the daily EventBridge sweep. The 7-day grace period is still enforced.

**Request**

```
POST /admin/archive
Authorization: Bearer <admin_token>
Content-Type: application/json
```

```json
{ "userId": "cognito-sub-uuid-abc-123" }
```

**Prerequisite:** User must already have `status=INACTIVE` and `archivePending=true` (set by Phase 1).

**200 OK**

```json
{ "message": "Archive complete for userId=cognito-sub-uuid-abc-123" }
```

**400 Bad Request** - userId missing, user not eligible, or 7-day window still open.

```json
{ "error": "Restore window still open. Phase 2 not permitted until 2026-08-23T02:14:37+00:00." }
```

**500 Internal Server Error - Verification failed (data safe)**

```json
{ "error": "Archive verification failed - hard-delete aborted. Archive file missing: archive/.../dynamodb/scene_data.json" }
```

Source data is untouched. Investigate S3 before retrying.

---

### 4.5 Error Catalog

| Status | Error | Cause | Action |
|---|---|---|---|
| `401` | `Authorization token missing` | No Authorization header | Fix request |
| `401` | `Invalid JWT format` | Token not 3 dot-separated parts | Fix token |
| `401` | `Missing 'sub' in token claims` | No sub claim in payload | Fix token |
| `400` | `userId is required` | Admin endpoint: userId empty | Fix request |
| `400` | `userId=... not found` | User doesn't exist or Phase 1 never ran | Run Phase 1 first |
| `400` | `userId=... is not INACTIVE` | User still active | Run Phase 1 first |
| `400` | `archivePending is not True` | Phase 2 already ran | No action needed |
| `400` | `Restore window still open` | Admin called Phase 2 within 7 days | Wait for window to close |
| `400` | `Restore window has expired` | >7 days since Phase 1 | Cannot restore |
| `400` | `Invalid or unknown deletion token` | Wrong token | Check the token |
| `409` | `Account already restored` | Restore already ran | No action needed |
| `500` | `Failed to process deletion request` | Phase 1 mark-INACTIVE failed | Retry Phase 1 |
| `500` | `Archive verification failed - hard-delete aborted` | S3 write/verify mismatch | Check S3, retry |
| `500` | *(other)* | Unexpected exception | Check CloudWatch logs |

---

## 5. Integration Guide

This section is for the **consuming team** building the deletion flow into a mobile or web app.

### Call sequence

```
1.  Authenticate        ->  obtain Cognito AccessToken
2.  Confirm with user   ->  explicit user action ("Delete my account")
3.  DELETE /account     ->  save deletionToken from response
4.  Clear local state   ->  tokens, cache, session -> redirect to post-deletion screen
5.  (Optional, <=7 days)->  POST /account/restore with token to cancel deletion
6.  (Background)        ->  Phase 2 auto-runs after 7 days via EventBridge
```

### Calling Phase 1

```bash
curl -X DELETE \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/{stage}/account" \
  -H "Authorization: Bearer <AccessToken>"
```

**On 200:** Save `deletionToken`. Clear all local tokens, cache, and session state. Redirect to post-deletion screen.

**On 401:** Token expired - re-authenticate and retry once.

**On 500:** Show a generic error. Retry is safe (Phase 1 is idempotent).

> After a successful 200 from Phase 1, do not attempt to refresh the token or call any other Digilux API with this user's credentials. The Cognito account is disabled.

### Restore (within 7 days)

Present the user with a restore option while within the 7-day window. They need their `deletionToken` (returned in the Phase 1 response - display it in the app or send it by email).

```bash
curl -X POST \
  "https://{api_id}.execute-api.ap-south-1.amazonaws.com/{stage}/account/restore" \
  -H "Content-Type: application/json" \
  -d '{"token": "f47ac10b-58cc-4372-a567-0e02b2c3d479"}'
```

On 200, the user can log in again immediately.

### Retry policy

| Scenario | Retry? | Action |
|---|---|---|
| Phase 1 -> 401 | Yes | Re-authenticate and retry once |
| Phase 1 -> 500 | Yes | Retry up to 3x, exponential back-off |
| Phase 1 -> 200 | No | Done |
| Restore -> 400 (window expired) | No | Data already deleted, cannot restore |
| Admin Phase 2 -> 400 (window open) | No | Wait for 7-day window to close |
| Admin Phase 2 -> 500 (verification failed) | Yes | Check S3, then retry |

### What NOT to do

| Do NOT | Why |
|---|---|
| Call DELETE /account without user confirmation | Irreversible after 7 days |
| Attempt token refresh after Phase 1 | Cognito is disabled. Refresh fails. |
| Call the admin archive endpoint within 7 days | Grace period enforced - returns 400 |
| Hard-code userId | Always extract from the `sub` JWT claim |
| Ignore a 500 on Phase 1 | User may still be active. Show error and let them retry. |

---

## 6. Audit Trail

### 6.1 deletion_audit table

Tracks the full deletion pipeline. One record per user.

**Key schema:** `userId` (PK, String) + `requestedAt` (SK, ISO-8601 timestamp)

**After Phase 1:**

```json
{
    "userId": "cognito-sub-uuid-abc-123",
    "requestedAt": "2026-08-16T02:14:37+00:00",
    "status": "PHASE1_COMPLETE",
    "phase1CompletedAt": "2026-08-16T02:14:37+00:00",
    "globalSignOutStatus": "ok",
    "cognitoDisableStatus": "ok",
    "devicesFound": 3,
    "devicesReleased": 3,
    "devicesReleaseFailed": 0,
    "deletedBy": "cognito-sub-uuid-abc-123"
}
```

**After Phase 2 (additional fields added):**

```json
{
    "status": "PHASE2_COMPLETE",
    "resolveCompletedAt": "2026-08-23T02:00:04+00:00",
    "archiveCompletedAt": "2026-08-23T02:00:07+00:00",
    "verifyCompletedAt": "2026-08-23T02:00:08+00:00",
    "hardDeleteCompletedAt": "2026-08-23T02:00:09+00:00",
    "phase2CompletedAt": "2026-08-23T02:00:09+00:00",
    "cognitoDeleteStatus": "ok",
    "subdevicesDeleted": 8,
    "tablesDeleted": {
        "scene_data": 4,
        "user_device_details": 1,
        "automation_schedule_controller": 7,
        "automation_schedule_direct": 3
    },
    "s3ObjectsDeleted": 12,
    "errors": []
}
```

| `status` | Meaning |
|---|---|
| `PHASE1_COMPLETE` | Phase 1 done, Phase 2 pending |
| `PHASE2_COMPLETE` | All data permanently deleted |
| `PHASE2_PARTIAL` | Phase 2 ran but some row deletes failed - check `errors[]` |
| `RESTORED` | User restored within the 7-day window |

---

### 6.2 deletion_evidence table

Compliance-grade record. Proves who initiated deletion, when, and from where. Email is stored KMS-encrypted (PII protection).

**Key schema:** `deletionToken` (PK, UUID)

```json
{
    "deletionToken": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "userId": "cognito-sub-uuid-abc-123",
    "emailEncrypted": "AQICAHh...base64-kms-ciphertext...==",
    "requestedAt": "2026-08-16T02:14:37+00:00",
    "ipAddress": "203.0.113.42",
    "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 ...)",
    "archiveStartedAt": null,
    "archiveCompletedAt": null,
    "archiveDeletedAt": null,
    "restoredAt": null
}
```

| Field | Description |
|---|---|
| `deletionToken` | UUID returned to user at Phase 1. Acts as proof-of-request and restore key. |
| `emailEncrypted` | User email encrypted with AWS KMS. Decryptable only by authorised roles. |
| `ipAddress` | Source IP captured from API Gateway request context. |
| `userAgent` | Browser/device string from request headers. |
| `archiveDeletedAt` | Set when Phase 2 completes - confirms when data was permanently destroyed. |
| `restoredAt` | Set if user restored. Null if deletion went through. |

---

### 6.3 CloudWatch audit events

Every step emits a structured JSON line to CloudWatch. Queryable via Logs Insights.

**Log groups:** `/aws/lambda/{env}-delete-account-phase1` and `/aws/lambda/{env}-delete-account-phase2`

| `audit_event` | Phase | Key fields |
|---|---|---|
| `DELETION_REQUEST_RECEIVED` | 1 | `userId`, `trigger` |
| `DELETION_ALREADY_PROCESSED` | 1 | `userId` |
| `GLOBAL_SIGN_OUT` | 1 | `userId`, `status` |
| `USER_MARKED_INACTIVE` | 1 | `userId`, `archivePending`, `markedAt` |
| `MARK_INACTIVE_FAILED` | 1 | `userId` |
| `DEVICES_RELEASED` | 1 | `userId`, `devicesFound`, `devicesReleased`, `devicesReleaseFailed` |
| `COGNITO_USER_DISABLED` | 1 | `userId`, `status` |
| `DELETION_EVIDENCE_WRITTEN` | 1 | `userId`, `deletionToken` |
| `PHASE1_COMPLETE` | 1 | `userId` + all step outcomes |
| `PHASE2_STARTED` | 2 | `userId` |
| `GRACE_PERIOD_REJECTED` | 2 | `userId`, `requestedAt`, `earliestRunAt` |
| `RESOLVE_COMPLETE` | 2 | `userId`, `devices`, `scenes`, `s3Objects` |
| `SUBDEVICE_SCAN_COMPLETE` | 2 | `userId`, `endpointFilesRead`, `subdeviceMacsFound`, `rowsToDelete` |
| `ARCHIVE_COMPLETE` | 2 | `userId`, `tablesArchived`, `s3ObjectsCopied` |
| `VERIFY_COMPLETE` | 2 | `userId`, `filesVerified` |
| `HARD_DELETE_COMPLETE` | 2 | `userId`, `tablesDeleted`, `subdevicesDeleted`, `s3ObjectsDeleted` |
| `HARD_DELETE_PARTIAL` | 2 | `userId`, `errors[]` |
| `COGNITO_USER_DELETED` | 2 | `userId`, `status` |
| `PHASE2_COMPLETE` | 2 | `userId` |
| `ACCOUNT_RESTORED` | Restore | `userId`, `deletionToken`, `restoredAt` |

**Example - trace full lifecycle for one user:**

```
fields @timestamp, audit_event, status, errors
| filter userId = "cognito-sub-uuid-abc-123"
| sort @timestamp asc
```

**Example - find verification failures (needs immediate attention):**

```
filter audit_event = "HARD_DELETE_PARTIAL" or message like "verification failed"
| fields @timestamp, userId, errors
| sort @timestamp desc
```

---

## 7. Monitoring & Alerting

### Recommended alarms

| Alarm | Pattern | Threshold | Action |
|---|---|---|---|
| Verification failure | `audit_event = "HARD_DELETE_PARTIAL"` | Count > 0 | Page on-call immediately |
| Phase 1 fatal failure | `audit_event = "MARK_INACTIVE_FAILED"` | Count > 0 | Alert platform team |
| Lambda errors (Phase 1) | Lambda `Errors` metric | > 2 in 5 min | Alert platform team |
| Lambda errors (Phase 2) | Lambda `Errors` metric | > 0 in sweep window | Alert platform team |
| High device release failures | `devicesReleaseFailed > 0` | Count > 5 in 1 hr | Alert device team |

### Useful queries

```sql
-- Deletions by hour (last 24 h)
filter audit_event = "PHASE1_COMPLETE"
| stats count() as total by bin(1h)

-- All restores in last 7 days
filter audit_event = "ACCOUNT_RESTORED"
| fields @timestamp, userId, deletionToken

-- Phase 2 results (complete vs partial)
filter audit_event = "PHASE2_COMPLETE" or audit_event = "HARD_DELETE_PARTIAL"
| fields @timestamp, audit_event, userId, errors
| sort @timestamp desc

-- Users blocked by grace period (admin called too early)
filter audit_event = "GRACE_PERIOD_REJECTED"
| fields @timestamp, userId, requestedAt, earliestRunAt
```

---

## 8. Troubleshooting

### Phase 1 issues

| Symptom | Cause | Fix |
|---|---|---|
| User gets 401 with valid-looking token | Token expired (1-hour TTL) | Re-authenticate and retry |
| User gets 500 on every retry | DynamoDB UpdateItem on user_data failing | Check Lambda CloudWatch logs; verify IAM allows `dynamodb:UpdateItem` on user_data |
| User can still log in after 200 response | `admin_disable_user` step failed (non-fatal) | Check `COGNITO_USER_DISABLED` log event; manually disable in Cognito if needed |
| `deletionToken` missing from response | Phase 1 Lambda not yet updated | Redeploy Phase 1 Lambda |

### Phase 2 issues

| Symptom | Cause | Fix |
|---|---|---|
| Phase 2 rejected with grace period error | Admin called within 7 days of Phase 1 | Wait for the window to close |
| `Archive verification failed` in logs | S3 write succeeded but HeadObject failed | Check S3 bucket permissions + `s3:HeadObject` IAM permission; retry |
| `PHASE2_PARTIAL` status | Some `DeleteItem` calls failed | Check `errors[]` in audit record; reset `archivePending=true` and retry Phase 2 |
| Subdevice rows not deleted | S3 endpoint files not found under userId/ | Verify files exist in metadata bucket; check `s3:ListBucket` IAM permission |
| EventBridge sweep not running | Schedule disabled or Lambda permission missing | Check `aws scheduler get-schedule`; check Lambda resource-based policy |

### Restore issues

| Symptom | Cause | Fix |
|---|---|---|
| Restore returns 400 "window expired" | >7 days since Phase 1 | Cannot restore - data was deleted |
| Restore returns 400 "invalid token" | Wrong token value | Check `deletion_evidence` table for the userId |
| User restored but cannot log in | `admin_enable_user` failed | Manually enable user in Cognito console |

### Full deletion verification checklist

```bash
# 1. Audit record shows PHASE2_COMPLETE
aws dynamodb query \
  --table-name digilux_honeywell_deletion_audit \
  --key-condition-expression "userId = :u" \
  --expression-attribute-values '{":u": {"S": "USER_ID"}}'

# 2. user_data shows INACTIVE + archivePending false
aws dynamodb get-item \
  --table-name digilux_honeywell_user_data \
  --key '{"userId": {"S": "USER_ID"}}'

# 3. Archive files exist in S3
aws s3 ls s3://digilux-honeywell-archive/archive/USER_ID/dynamodb/ --recursive

# 4. Cognito user is gone
aws cognito-idp admin-get-user \
  --user-pool-id ap-south-1_KJpJMEzyM \
  --username USER_ID
# Expected: UserNotFoundException
```

---

## 9. Deployment Guide (DevOps Handover)

This section is for the **DevOps / Infrastructure team** responsible for deploying and maintaining this feature. Everything needed to go from zero to production is here.

---

### 9.1 Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| AWS CLI | v2.x | `brew install awscli` |
| Terraform | >= 1.6.0 | `brew tap hashicorp/tap && brew install hashicorp/tap/terraform` |
| Python | 3.12 | `brew install python@3.12` |
| make | any | Pre-installed on macOS/Linux |

**AWS CLI configuration:**

```bash
aws configure --profile digilux
# AWS Access Key ID:     <your key>
# AWS Secret Access Key: <your secret>
# Default region:        ap-south-1
# Default output format: json

export AWS_PROFILE=digilux
```

For Honeywell client: `aws configure --profile honeywell` and `export AWS_PROFILE=honeywell`.

**Minimum IAM permissions for the deploying user/role:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "dynamodb:*",        "Resource": "*" },
    { "Effect": "Allow", "Action": "s3:*",              "Resource": "*" },
    { "Effect": "Allow", "Action": "lambda:*",          "Resource": "*" },
    { "Effect": "Allow", "Action": "apigateway:*",      "Resource": "*" },
    { "Effect": "Allow", "Action": "iam:*",             "Resource": "*" },
    { "Effect": "Allow", "Action": "scheduler:*",       "Resource": "*" },
    { "Effect": "Allow", "Action": "logs:*",            "Resource": "*" },
    { "Effect": "Allow", "Action": "cloudwatch:*",      "Resource": "*" },
    { "Effect": "Allow", "Action": "sns:*",             "Resource": "*" },
    { "Effect": "Allow", "Action": "cognito-idp:List*", "Resource": "*" }
  ]
}
```

---

### 9.2 Repository structure

```
digilux-delete-account/
+-- Makefile                          <- All deployment commands
+-- phase1/lambda_function.py         <- Phase 1 Lambda
+-- phase2/
|   +-- lambda_function.py            <- Phase 2 handler
|   +-- archiver.py                   <- Archive/delete engine
+-- infra/
|   +-- main.tf                       <- Provider + S3 backend
|   +-- variables.tf                  <- Input variables
|   +-- locals.tf                     <- Naming conventions
|   +-- dynamodb.tf                   <- deletion_audit + deletion_evidence tables
|   +-- s3.tf                         <- Archive S3 bucket
|   +-- iam.tf                        <- IAM roles + policies
|   +-- lambda.tf                     <- Lambda functions (auto-zips source)
|   +-- api_gateway.tf                <- REST API + authorizer + CORS
|   +-- eventbridge.tf                <- Daily 02:00 UTC sweep
|   +-- cloudwatch.tf                 <- Log groups + alarms
|   +-- outputs.tf                    <- API URL, ARNs, etc.
|   +-- envs/
|       +-- digilux.tfvars            <- Digilux variables
|       +-- digilux-backend.hcl       <- Digilux Terraform state config
|       +-- honeywell.tfvars          <- Honeywell variables
|       +-- honeywell-backend.hcl     <- Honeywell state config
+-- tests/                            <- Unit tests
+-- postman/                          <- Postman collection
+-- docs/ACCOUNT_DELETION.md          <- This document
```

---

### 9.3 One-time bootstrap (first deploy only)

Terraform state is stored in S3 with DynamoDB locking. Create these once per AWS account before any Terraform run.

```bash
# State bucket
aws s3 mb s3://digilux-terraform-state --region ap-south-1
aws s3api put-bucket-versioning \
    --bucket digilux-terraform-state \
    --versioning-configuration Status=Enabled
aws s3api put-public-access-block \
    --bucket digilux-terraform-state \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Lock table
aws dynamodb create-table \
    --table-name terraform-state-lock \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region ap-south-1
```

---

### 9.4 Clone the repository

```bash
git clone https://github.com/mahesh-maney/digilux-delete-account.git
cd digilux-delete-account
```

---

### 9.5 Deploy to Digilux

1. Review `infra/envs/digilux.tfvars` - confirm `cognito_user_pool_id` and set `alert_email`.
2. Run tests: `python3 -m unittest discover tests/ -v` - all must pass.
3. Deploy: `make deploy ENV=digilux`
4. Collect outputs: `make output ENV=digilux`
5. Get admin API key: `terraform -chdir=infra output -raw admin_api_key`

---

### 9.6 Deploy to Honeywell

1. Find Honeywell Cognito User Pool: `aws cognito-idp list-user-pools --max-results 20`
2. Set `cognito_user_pool_id` in `infra/envs/honeywell.tfvars`
3. Switch profile if different AWS account: `export AWS_PROFILE=honeywell`
4. Deploy: `make deploy ENV=honeywell`

---

### 9.7 Adding a future client environment

```bash
cp infra/envs/honeywell.tfvars      infra/envs/acme.tfvars
cp infra/envs/honeywell-backend.hcl infra/envs/acme-backend.hcl
# Edit acme.tfvars: set env, cognito_user_pool_id, alert_email
# Edit acme-backend.hcl: set key = "delete-account/acme/terraform.tfstate"
make deploy ENV=acme
```

No Terraform code changes needed - all naming is driven by `var.env`.

---

### 9.8 Updating Lambda code

```bash
python3 -m unittest discover tests/ -v  # must pass first
make deploy ENV=digilux                 # Terraform detects hash change and re-uploads Lambda
```

---

### 9.9 Post-deployment verification

```bash
aws lambda get-function --function-name digilux-delete-account-phase1
aws lambda get-function --function-name digilux-delete-account-phase2
aws dynamodb describe-table --table-name digilux_honeywell_deletion_audit
aws dynamodb describe-table --table-name digilux_honeywell_deletion_evidence
aws s3 ls s3://digilux-honeywell-archive
aws scheduler get-schedule --name digilux-delete-account-daily-sweep
```

Run the Postman collection - negative tests only (do not run happy-path deletion against a real user in production):

```
Auth -> Get Cognito Token           -> 200
Phase 1 -> No Auth Header           -> 401
Phase 2 -> Missing userId           -> 400
CORS -> OPTIONS /account            -> 200 with CORS headers
```

---

### 9.10 Makefile reference

| Command | What it does |
|---|---|
| `make deploy ENV=digilux` | Full deploy: init -> plan -> apply -> outputs |
| `make plan ENV=digilux` | Generate plan only |
| `make apply ENV=digilux` | Apply a saved plan |
| `make output ENV=digilux` | Print Terraform outputs |
| `make fmt` | Auto-format all `.tf` files |
| `make validate ENV=digilux` | Validate HCL syntax |
| `make destroy ENV=digilux` | Destroy resources (see warning below) |

---

### 9.11 Destroying an environment

```bash
make destroy ENV=digilux
```

> The `deletion_audit` table, `deletion_evidence` table, and `archive` S3 bucket have `prevent_destroy = true`. They will **not** be destroyed. This is intentional - audit trails and archived data must never be accidentally lost. To destroy them, manually remove the `lifecycle` blocks, apply, then destroy. **Never do this in production without explicit written approval.**

---

### 9.12 Environment variables injected into Lambda

All set automatically by Terraform. No manual configuration needed.

**Phase 1 Lambda:**

| Variable | Value (digilux) |
|---|---|
| `USER_POOL_ID` | `ap-south-1_KJpJMEzyM` |
| `TABLE_USER_DATA` | `digilux_honeywell_user_data` |
| `TABLE_DEVICE_DATA` | `digilux_honeywell_device_data` |
| `TABLE_DELETION_AUDIT` | `digilux_honeywell_deletion_audit` |
| `TABLE_DELETION_EVIDENCE` | `digilux_honeywell_deletion_evidence` |

**Phase 2 Lambda:**

| Variable | Value (digilux) |
|---|---|
| `TABLE_USER_DATA` | `digilux_honeywell_user_data` |
| `TABLE_DEVICE_DATA` | `digilux_honeywell_device_data` |
| `TABLE_SCENE_DATA` | `digilux_honeywell_scene_data` |
| `TABLE_USER_DEVICE_DETAILS` | `digilux_honeywell_user_device_details` |
| `TABLE_USER_DEVICE_MAPPING` | `digilux_honeywell_user_device_mapping` |
| `TABLE_USER_SUBUSER_DETAIL` | `digilux_honeywell_user_subuser_detail` |
| `TABLE_USER_SUBUSER_MAPPING` | `digilux_honeywell_user_subuser_mapping` |
| `TABLE_SUBUSER_ROLE_DATA` | `digilux_honeywell_subuser_role_data` |
| `TABLE_ADMIN_OTP_DATA` | `digilux_honeywell_admin_otp_data` |
| `TABLE_ALEXA_LWA_TOKENS` | `digilux_honeywell_alexa_lwa_tokens` |
| `TABLE_DEVICE_STATE` | `digilux_honeywell_device_state` |
| `TABLE_ENTITY_STATE` | `digilux_honeywell_entity_state` |
| `TABLE_AUTOMATION_EVENT` | `digilux_honeywell_automation_event` |
| `TABLE_AUTOMATION_SCHEDULE_DIRECT` | `digilux_honeywell_automation_schedule_direct` |
| `TABLE_AUTOMATION_SCHEDULE_CTRL` | `digilux_honeywell_automation_schedule_controller` |
| `TABLE_DELETION_AUDIT` | `digilux_honeywell_deletion_audit` |
| `TABLE_DELETION_EVIDENCE` | `digilux_honeywell_deletion_evidence` |
| `ARCHIVE_BUCKET` | `digilux-honeywell-archive` |
| `METADATA_BUCKET` | `digilux-honeywell-metadata` |

---

### 9.13 Rollback procedure

**Roll back Lambda to a previous commit:**

```bash
git checkout <previous-commit-hash> -- phase1/ phase2/
make deploy ENV=digilux
git checkout HEAD -- phase1/ phase2/
```

**Fix corrupted Terraform state:**

```bash
terraform -chdir=infra state list
terraform -chdir=infra state rm aws_lambda_function.phase1
make plan ENV=digilux   # confirm state is clean
```

---

### 9.14 Common deployment errors

**`Error: S3 bucket not found` on `terraform init`**
State bucket does not exist. Run Section 9.3 bootstrap commands.

**`InvalidParameterValueException: runtime not supported`**
Update `lambda_runtime` in `.tfvars` to a supported Python version (e.g., `python3.12`).

**`AccessDeniedException: not authorized to perform: iam:CreateRole`**
Attach the IAM policy from Section 9.1 to the deploying user.

**`BadRequestException: REST API doesn't contain any methods`**
Re-run `make plan` then `make apply`. Terraform ordering issue that resolves on the second pass.

**`ConflictException: Schedule already exists`**
Import into state:
```bash
terraform -chdir=infra import -var-file=envs/digilux.tfvars \
    aws_scheduler_schedule.daily_sweep \
    default/digilux-delete-account-daily-sweep
```

---

### 9.15 Support escalation

| Issue | Contact |
|---|---|
| Terraform state corruption | Platform team (Mahesh) |
| Lambda code bug | Platform team (Mahesh) |
| Cognito User Pool ID | Client account owner / Honeywell team |
| AWS account credentials | DevOps lead |
| API / integration questions | See Section 4 of this document |

---

*For questions or escalations, contact the Digilux Platform Team.*
