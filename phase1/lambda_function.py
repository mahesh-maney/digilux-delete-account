import json
import os
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
    f"TABLE_DEVICE_DATA={os.environ.get('TABLE_DEVICE_DATA', 'digilux_honeywell_device_data')} "
    f"TABLE_DELETION_AUDIT={os.environ.get('TABLE_DELETION_AUDIT', 'digilux_honeywell_deletion_audit')}"
)

# ── Environment variables ──────────────────────────────────────────────────────
USER_POOL_ID         = os.environ.get("USER_POOL_ID",          "ap-south-1_KJpJMEzyM")
TABLE_USER_DATA      = os.environ.get("TABLE_USER_DATA",       "digilux_honeywell_user_data")
TABLE_DEVICE_DATA    = os.environ.get("TABLE_DEVICE_DATA",     "digilux_honeywell_device_data")
TABLE_DELETION_AUDIT = os.environ.get("TABLE_DELETION_AUDIT",  "digilux_honeywell_deletion_audit")

ACK_MESSAGE = (
    "Your account deletion request has been received. "
    "Your access has been revoked immediately. "
    "All associated data will be permanently removed within 7 business days."
)


# ── Audit helper ───────────────────────────────────────────────────────────────

def _audit_event(event_type, user_id, **fields):
    """
    Emit a structured JSON audit line to CloudWatch.
    Queryable via CloudWatch Logs Insights:
      filter audit_event = "GLOBAL_SIGN_OUT" | fields userId, status, timestamp
    """
    logger.info(json.dumps({
        "audit_event": event_type,
        "userId":      user_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        **fields,
    }))


# ── Entry point ────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    logger.info("phase1_account_deletion invoked")

    # 1. Auth — extract JWT, get userId from 'sub' claim
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

    # 2. Idempotency — if already INACTIVE, return early
    try:
        item = dynamodb.Table(TABLE_USER_DATA).get_item(Key={"userId": user_id}).get("Item")
        if not item:
            logger.info(f"[{user_id}] No user_data record found — proceeding with deletion")
        elif item.get("status") == "INACTIVE":
            logger.info(f"[{user_id}] Already INACTIVE — returning early")
            _audit_event("DELETION_ALREADY_PROCESSED", user_id)
            return _resp(200, {"message": ACK_MESSAGE, "status": "already_processed"})
        else:
            logger.info(f"[{user_id}] Current status={item.get('status')!r} — proceeding")
    except Exception:
        logger.exception(f"[{user_id}] Idempotency check failed — continuing")

    now = datetime.now(timezone.utc).isoformat()

    # 3. Global sign-out — revoke all active sessions immediately
    sign_out_status = "not_attempted"
    try:
        cognito_idp.admin_user_global_sign_out(UserPoolId=USER_POOL_ID, Username=user_id)
        sign_out_status = "ok"
        logger.info(f"[{user_id}] Global sign-out done")
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="ok")
    except cognito_idp.exceptions.UserNotFoundException:
        sign_out_status = "user_not_found"
        logger.warning(f"[{user_id}] Cognito user not found during sign-out — continuing")
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="user_not_found")
    except Exception:
        sign_out_status = "error"
        logger.exception(f"[{user_id}] Global sign-out failed — continuing")
        _audit_event("GLOBAL_SIGN_OUT", user_id, status="error")

    # 4. Mark user INACTIVE + flag for Phase 2 archive
    try:
        dynamodb.Table(TABLE_USER_DATA).update_item(
            Key={"userId": user_id},
            UpdateExpression=(
                "SET #s = :inactive, deletionRequestedAt = :ts, "
                "archivePending = :true, deletedBy = :by"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":inactive": "INACTIVE",
                ":ts":       now,
                ":true":     True,
                ":by":       user_id,
            },
        )
        logger.info(f"[{user_id}] Marked INACTIVE + archivePending=True")
        _audit_event("USER_MARKED_INACTIVE", user_id, archivePending=True, markedAt=now)
    except Exception:
        logger.exception(f"[{user_id}] Failed to mark INACTIVE — aborting")
        _audit_event("MARK_INACTIVE_FAILED", user_id)
        return _resp(500, {"message": "Failed to process deletion request. Please try again."})

    # 5. Release devices — set userId = "0" so devices are unowned, not deleted
    devices_found = devices_released = devices_release_failed = 0
    try:
        devices = _query_all(
            TABLE_DEVICE_DATA,
            index="userId-index",
            key_attr="userId",
            key_val=user_id,
        )
        devices_found = len(devices)
        logger.info(f"[{user_id}] Found {devices_found} devices to release")
        for d in devices:
            device_id = d.get("deviceId")
            try:
                dynamodb.Table(TABLE_DEVICE_DATA).update_item(
                    Key={"deviceId": device_id, "macAddress": d["macAddress"]},
                    UpdateExpression="SET userId = :v",
                    ExpressionAttributeValues={":v": "0"},
                )
                devices_released += 1
                logger.debug(f"[{user_id}] Released device {device_id}")
            except Exception:
                devices_release_failed += 1
                logger.exception(f"[{user_id}] Failed to release device {device_id} — continuing")
        logger.info(
            f"[{user_id}] Device release done — "
            f"found={devices_found} released={devices_released} failed={devices_release_failed}"
        )
        _audit_event("DEVICES_RELEASED", user_id,
                     devicesFound=devices_found,
                     devicesReleased=devices_released,
                     devicesReleaseFailed=devices_release_failed)
    except Exception:
        logger.exception(f"[{user_id}] Device query failed — devices not released")
        _audit_event("DEVICES_RELEASE_FAILED", user_id, reason="query_error")

    # 6. Delete Cognito user — login now impossible
    cognito_status = "not_attempted"
    try:
        cognito_idp.admin_delete_user(UserPoolId=USER_POOL_ID, Username=user_id)
        cognito_status = "ok"
        logger.info(f"[{user_id}] Cognito user deleted")
        _audit_event("COGNITO_USER_DELETED", user_id, status="ok")
    except cognito_idp.exceptions.UserNotFoundException:
        cognito_status = "already_absent"
        logger.warning(f"[{user_id}] Cognito user already absent")
        _audit_event("COGNITO_USER_DELETED", user_id, status="already_absent")
    except Exception:
        cognito_status = "error"
        logger.exception(f"[{user_id}] Cognito delete failed — continuing")
        _audit_event("COGNITO_USER_DELETED", user_id, status="error")

    # 7. Write audit log entry — captures every step outcome in one DynamoDB record
    try:
        dynamodb.Table(TABLE_DELETION_AUDIT).put_item(Item={
            "userId":               user_id,
            "requestedAt":          now,
            "status":               "PHASE1_COMPLETE",
            "phase1CompletedAt":    now,
            "globalSignOutStatus":  sign_out_status,
            "devicesFound":         devices_found,
            "devicesReleased":      devices_released,
            "devicesReleaseFailed": devices_release_failed,
            "cognitoDeleteStatus":  cognito_status,
            "deletedBy":            user_id,
        })
        logger.info(f"[{user_id}] Audit log written")
        _audit_event("PHASE1_COMPLETE", user_id,
                     globalSignOutStatus=sign_out_status,
                     devicesFound=devices_found,
                     devicesReleased=devices_released,
                     devicesReleaseFailed=devices_release_failed,
                     cognitoDeleteStatus=cognito_status)
    except Exception:
        logger.exception(f"[{user_id}] Audit log write failed — non-fatal")

    logger.info(f"[{user_id}] Phase 1 complete")
    return _resp(200, {"message": ACK_MESSAGE, "status": "processing"})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _query_all(table_name, key_attr, key_val, index=None):
    """Query a DynamoDB table (or GSI) by a single key attribute, handles pagination."""
    table  = dynamodb.Table(table_name)
    kwargs = {
        "KeyConditionExpression": boto3.dynamodb.conditions.Key(key_attr).eq(key_val)
    }
    if index:
        kwargs["IndexName"] = index

    items, page = [], 0
    while True:
        resp = table.query(**kwargs)
        page_items = resp.get("Items", [])
        items.extend(page_items)
        page += 1
        lek = resp.get("LastEvaluatedKey")
        logger.debug(f"_query_all {table_name} page={page} items_this_page={len(page_items)} has_more={bool(lek)}")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    logger.debug(f"_query_all {table_name} total={len(items)} pages={page}")
    return items


def _extract_token(event):
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization")
    return auth.replace("Bearer ", "").strip() if auth else None


def _decode_token(token):
    """Decode Cognito JWT and return the 'sub' claim as userId."""
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
            "Access-Control-Allow-Methods": "DELETE,OPTIONS",
            "Content-Type":                 "application/json",
        },
        "body": json.dumps(body),
    }
