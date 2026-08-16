import json
import os
import uuid
import boto3
import base64
import logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients ────────────────────────────────────────────────────────────────
dynamodb    = boto3.resource("dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None)
cognito_idp = boto3.client("cognito-idp")

logger.info(
    "phase1 config loaded — "
    f"USER_POOL_ID={os.environ.get('USER_POOL_ID', 'ap-south-1_KJpJMEzyM')} "
    f"TABLE_USER_DATA={os.environ.get('TABLE_USER_DATA', 'digilux_honeywell_user_data')} "
    f"TABLE_DELETION_AUDIT={os.environ.get('TABLE_DELETION_AUDIT', 'digilux_honeywell_deletion_audit')}"
)

# ── Environment variables ──────────────────────────────────────────────────────
USER_POOL_ID          = os.environ.get("USER_POOL_ID",           "ap-south-1_KJpJMEzyM")
TABLE_USER_DATA       = os.environ.get("TABLE_USER_DATA",        "digilux_honeywell_user_data")
TABLE_DEVICE_DATA     = os.environ.get("TABLE_DEVICE_DATA",      "digilux_honeywell_device_data")
TABLE_DELETION_AUDIT  = os.environ.get("TABLE_DELETION_AUDIT",   "digilux_honeywell_deletion_audit")
TABLE_DELETION_EVIDENCE = os.environ.get("TABLE_DELETION_EVIDENCE", "digilux_honeywell_deletion_evidence")

ACK_MESSAGE = (
    "Your account deletion request has been received. "
    "Your access has been revoked immediately. "
    "All associated data will be permanently removed within 7 business days."
)


# ── Audit helper ───────────────────────────────────────────────────────────────

def _audit_event(event_type, user_id, **fields):
    logger.info(json.dumps({
        "audit_event": event_type,
        "userId":      user_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        **fields,
    }))


# ── PII helper ─────────────────────────────────────────────────────────────────

def _encode_pii(value):
    """Base64-encode PII for storage. Replace with AWS KMS in production."""
    if not value:
        return None
    return base64.b64encode(value.encode()).decode()


def _get_user_email(user_id):
    """Try to fetch email attribute from Cognito. Non-fatal if unavailable."""
    try:
        resp = cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
        for attr in resp.get("UserAttributes", []):
            if attr["Name"] == "email":
                return attr["Value"]
    except Exception:
        logger.warning(f"[{user_id}] Could not fetch email from Cognito — PII will be absent from deletion_evidence")
    return None


# ── Entry point ────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    method = (event.get("httpMethod") or "DELETE").upper()
    path   = event.get("path") or ""

    logger.info(f"phase1 invoked — method={method!r} path={path!r}")

    # Route: GET /account/deletion-status
    if method == "GET" and "deletion-status" in path:
        return _handle_deletion_status(event)

    # Default: DELETE /account
    return _handle_phase1(event)


# ── GET /account/deletion-status ──────────────────────────────────────────────

def _handle_deletion_status(event):
    """Return deletion timeline for a given deletionToken. No auth required."""
    params = (event.get("queryStringParameters") or {})
    token  = (params.get("token") or "").strip()
    if not token:
        return _resp(400, {"error": "Invalid or unknown deletion token."})

    try:
        item = dynamodb.Table(TABLE_DELETION_EVIDENCE).get_item(
            Key={"deletionToken": token}
        ).get("Item")
    except Exception:
        logger.exception("Failed to query deletion_evidence")
        return _resp(500, {"error": "An unexpected error occurred."})

    if not item:
        logger.warning(f"deletion-status queried with unknown token — token={token[:8]}...")
        return _resp(400, {"error": "Invalid or unknown deletion token."})

    user_id_from_evidence = item.get("userId", "unknown")
    _audit_event("DELETION_STATUS_QUERIED", user_id_from_evidence, tokenPrefix=token[:8])
    logger.info(f"[{user_id_from_evidence}] Deletion status queried — token={token[:8]}...")

    if item.get("restoredAt"):
        status = "RESTORED"
    elif item.get("archiveDeletedAt"):
        status = "PHASE2_COMPLETE"
    else:
        status = "PHASE1_COMPLETE"

    return _resp(200, {
        "userId":            item.get("userId"),
        "requestedAt":       item.get("requestedAt"),
        "ipAddress":         item.get("ipAddress"),
        "userAgent":         item.get("userAgent"),
        "archiveStartedAt":  item.get("archiveStartedAt"),
        "archiveCompletedAt": item.get("archiveCompletedAt"),
        "archiveDeletedAt":  item.get("archiveDeletedAt"),
        "restoredAt":        item.get("restoredAt"),
        "status":            status,
    })


# ── DELETE /account (Phase 1) ──────────────────────────────────────────────────

def _handle_phase1(event):
    logger.info("phase1_account_deletion invoked")

    # 1. Auth
    try:
        token = _extract_token(event)
        if not token:
            return _resp(401, {"message": "Unauthorized", "error": "Authorization token missing"})
        user_id = _decode_token(token)
    except Exception as e:
        logger.exception("JWT decode failed")
        return _resp(401, {"message": "Unauthorized", "error": str(e)})

    logger.info(f"Self-deletion request — userId={user_id}")
    _audit_event("DELETION_REQUEST_RECEIVED", user_id, trigger="self")

    # 2. Idempotency
    try:
        item = dynamodb.Table(TABLE_USER_DATA).get_item(Key={"userId": user_id}).get("Item")
        if not item:
            logger.info(f"[{user_id}] No user_data record found — proceeding")
        elif item.get("status") == "INACTIVE":
            logger.info(f"[{user_id}] Already INACTIVE — returning early")
            _audit_event("DELETION_ALREADY_PROCESSED", user_id)
            return _resp(200, {"message": ACK_MESSAGE, "status": "already_processed"})
    except Exception:
        logger.exception(f"[{user_id}] Idempotency check failed — continuing")

    now            = datetime.now(timezone.utc).isoformat()
    deletion_token = str(uuid.uuid4())
    ip_address     = ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp", "unknown")
    headers        = event.get("headers") or {}
    user_agent     = headers.get("User-Agent") or headers.get("user-agent", "unknown")

    # 3. Global sign-out
    sign_out_status = "not_attempted"
    try:
        cognito_idp.admin_user_global_sign_out(UserPoolId=USER_POOL_ID, Username=user_id)
        sign_out_status = "ok"
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="ok")
    except cognito_idp.exceptions.UserNotFoundException:
        sign_out_status = "user_not_found"
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="user_not_found")
    except Exception:
        sign_out_status = "error"
        logger.exception(f"[{user_id}] Global sign-out failed — continuing")
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="error")

    # 4. Mark INACTIVE (only fatal step)
    try:
        dynamodb.Table(TABLE_USER_DATA).update_item(
            Key={"userId": user_id},
            UpdateExpression=(
                "SET #s = :inactive, deletionRequestedAt = :ts, "
                "archivePending = :true, deletedBy = :by, deletionToken = :tok"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":inactive": "INACTIVE",
                ":ts":       now,
                ":true":     True,
                ":by":       user_id,
                ":tok":      deletion_token,
            },
        )
        logger.info(f"[{user_id}] Marked INACTIVE + archivePending=True")
        _audit_event("USER_MARKED_INACTIVE", user_id, archivePending=True, markedAt=now)
    except Exception:
        logger.exception(f"[{user_id}] Failed to mark INACTIVE — aborting")
        _audit_event("MARK_INACTIVE_FAILED", user_id)
        return _resp(500, {"message": "Failed to process deletion request. Please try again."})

    # 5. Unbind devices (userId → "0", device hardware stays in system)
    devices_found = devices_released = devices_release_failed = 0
    device_bindings = []
    try:
        devices = _query_all(
            TABLE_DEVICE_DATA,
            index="userId-index",
            key_attr="userId",
            key_val=user_id,
        )
        devices_found = len(devices)
        logger.info(f"[{user_id}] Found {devices_found} devices to unbind")

        for d in devices:
            device_id  = d.get("deviceId")
            mac        = d.get("macAddress")
            if device_id and mac:
                device_bindings.append({"deviceId": device_id, "macAddress": mac})
            try:
                dynamodb.Table(TABLE_DEVICE_DATA).update_item(
                    Key={"deviceId": device_id, "macAddress": mac},
                    UpdateExpression="SET userId = :v",
                    ExpressionAttributeValues={":v": "0"},
                )
                devices_released += 1
            except Exception:
                devices_release_failed += 1
                logger.exception(f"[{user_id}] Failed to unbind device {device_id} — continuing")

        # Save bindings backup for restore
        if device_bindings:
            try:
                dynamodb.Table(TABLE_USER_DATA).update_item(
                    Key={"userId": user_id},
                    UpdateExpression="SET deviceBindingsBackup = :b",
                    ExpressionAttributeValues={":b": device_bindings},
                )
                logger.info(f"[{user_id}] Device bindings backup saved — {len(device_bindings)} entries")
            except Exception:
                logger.exception(f"[{user_id}] Failed to save device bindings backup — non-fatal")

        if devices_release_failed > 0:
            logger.warning(
                f"[{user_id}] {devices_release_failed}/{devices_found} device(s) failed to unbind — "
                "restore may not re-bind all devices correctly"
            )

        _audit_event("DEVICES_RELEASED", user_id,
                     devicesFound=devices_found,
                     devicesReleased=devices_released,
                     devicesReleaseFailed=devices_release_failed)
    except Exception:
        logger.exception(f"[{user_id}] Device query failed — devices not unbound")
        _audit_event("DEVICES_RELEASE_FAILED", user_id, reason="query_error")

    # 6. Disable Cognito user (login blocked; user kept for 7-day restore window)
    cognito_disable_status = "not_attempted"
    try:
        cognito_idp.admin_disable_user(UserPoolId=USER_POOL_ID, Username=user_id)
        cognito_disable_status = "ok"
        _audit_event("COGNITO_USER_DISABLED", user_id, status="ok")
    except cognito_idp.exceptions.UserNotFoundException:
        cognito_disable_status = "user_not_found"
        _audit_event("COGNITO_USER_DISABLED", user_id, status="user_not_found")
    except Exception:
        cognito_disable_status = "error"
        logger.exception(f"[{user_id}] Cognito disable failed — continuing")
        _audit_event("COGNITO_USER_DISABLED", user_id, status="error")

    # 7. Write deletion_evidence (compliance trail with encrypted PII)
    try:
        email         = _get_user_email(user_id)
        email_encoded = _encode_pii(email)
        evidence_item = {
            "deletionToken": deletion_token,
            "userId":        user_id,
            "requestedAt":   now,
            "ipAddress":     ip_address,
            "userAgent":     user_agent,
        }
        if email_encoded:
            evidence_item["emailEncrypted"] = email_encoded
        dynamodb.Table(TABLE_DELETION_EVIDENCE).put_item(Item=evidence_item)
        _audit_event("DELETION_EVIDENCE_WRITTEN", user_id, deletionToken=deletion_token)
    except Exception:
        logger.exception(f"[{user_id}] Failed to write deletion_evidence — non-fatal")

    # 8. Write audit log
    try:
        dynamodb.Table(TABLE_DELETION_AUDIT).put_item(Item={
            "userId":                user_id,
            "requestedAt":           now,
            "status":                "PHASE1_COMPLETE",
            "phase1CompletedAt":     now,
            "globalSignOutStatus":   sign_out_status,
            "cognitoDisableStatus":  cognito_disable_status,
            "devicesFound":          devices_found,
            "devicesReleased":       devices_released,
            "devicesReleaseFailed":  devices_release_failed,
            "deletedBy":             user_id,
        })
        _audit_event("PHASE1_COMPLETE", user_id,
                     globalSignOutStatus=sign_out_status,
                     cognitoDisableStatus=cognito_disable_status,
                     devicesFound=devices_found,
                     devicesReleased=devices_released,
                     devicesReleaseFailed=devices_release_failed)
    except Exception:
        logger.exception(f"[{user_id}] Audit log write failed — non-fatal")

    logger.info(f"[{user_id}] Phase 1 complete")
    return _resp(200, {
        "message":       ACK_MESSAGE,
        "status":        "processing",
        "deletionToken": deletion_token,
    })


# ── Helpers ────────────────────────────────────────────────────────────────────

def _query_all(table_name, key_attr, key_val, index=None):
    table  = dynamodb.Table(table_name)
    kwargs = {"KeyConditionExpression": boto3.dynamodb.conditions.Key(key_attr).eq(key_val)}
    if index:
        kwargs["IndexName"] = index
    items, page = [], 0
    while True:
        resp       = table.query(**kwargs)
        page_items = resp.get("Items", [])
        items.extend(page_items)
        page += 1
        lek = resp.get("LastEvaluatedKey")
        logger.debug(f"_query_all {table_name} page={page} this_page={len(page_items)} total={len(items)} has_more={bool(lek)}")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _extract_token(event):
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization")
    return auth.replace("Bearer ", "").strip() if auth else None


def _decode_token(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    user_id = claims.get("sub")
    if not user_id:
        raise ValueError("Missing 'sub' in token claims")
    return user_id


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "DELETE,GET,OPTIONS",
            "Content-Type":                 "application/json",
        },
        "body": json.dumps(body),
    }
