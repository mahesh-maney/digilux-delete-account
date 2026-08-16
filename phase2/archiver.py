import json
import os
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ── Environment ────────────────────────────────────────────────────────────────
ARCHIVE_BUCKET                   = os.environ.get("ARCHIVE_BUCKET",                   "digilux-honeywell-archive")
METADATA_BUCKET                  = os.environ.get("METADATA_BUCKET",                  "digilux-honeywell-metadata")

TABLE_USER_DATA                  = os.environ.get("TABLE_USER_DATA",                  "digilux_honeywell_user_data")
TABLE_DEVICE_DATA                = os.environ.get("TABLE_DEVICE_DATA",                "digilux_honeywell_device_data")
TABLE_SCENE_DATA                 = os.environ.get("TABLE_SCENE_DATA",                 "digilux_honeywell_scene_data")
TABLE_USER_DEVICE_DETAILS        = os.environ.get("TABLE_USER_DEVICE_DETAILS",        "digilux_honeywell_user_device_details")
TABLE_USER_DEVICE_MAPPING        = os.environ.get("TABLE_USER_DEVICE_MAPPING",        "digilux_honeywell_user_device_mapping")
TABLE_USER_SUBUSER_DETAIL        = os.environ.get("TABLE_USER_SUBUSER_DETAIL",        "digilux_honeywell_user_subuser_detail")
TABLE_USER_SUBUSER_MAPPING       = os.environ.get("TABLE_USER_SUBUSER_MAPPING",       "digilux_honeywell_user_subuser_mapping")
TABLE_SUBUSER_ROLE_DATA          = os.environ.get("TABLE_SUBUSER_ROLE_DATA",          "digilux_honeywell_subuser_role_data")
TABLE_ADMIN_OTP_DATA             = os.environ.get("TABLE_ADMIN_OTP_DATA",             "digilux_honeywell_admin_otp_data")
TABLE_ALEXA_LWA_TOKENS           = os.environ.get("TABLE_ALEXA_LWA_TOKENS",           "digilux_honeywell_alexa_lwa_tokens")
TABLE_DEVICE_STATE               = os.environ.get("TABLE_DEVICE_STATE",               "digilux_honeywell_device_state")
TABLE_ENTITY_STATE               = os.environ.get("TABLE_ENTITY_STATE",               "digilux_honeywell_entity_state")
TABLE_AUTOMATION_EVENT           = os.environ.get("TABLE_AUTOMATION_EVENT",           "digilux_honeywell_automation_event")
TABLE_AUTOMATION_SCHEDULE_DIRECT = os.environ.get("TABLE_AUTOMATION_SCHEDULE_DIRECT", "digilux_honeywell_automation_schedule_direct")
TABLE_AUTOMATION_SCHEDULE_CTRL   = os.environ.get("TABLE_AUTOMATION_SCHEDULE_CTRL",   "digilux_honeywell_automation_schedule_controller")
TABLE_DELETION_AUDIT             = os.environ.get("TABLE_DELETION_AUDIT",             "digilux_honeywell_deletion_audit")


# ── Audit helper ───────────────────────────────────────────────────────────────

def _audit_event(event_type, user_id, **fields):
    """
    Emit a structured JSON audit line to CloudWatch.
    Queryable via CloudWatch Logs Insights:
      filter audit_event = "ARCHIVE_COMPLETE" | fields userId, s3ObjectsCopied, timestamp
    """
    logger.info(json.dumps({
        "audit_event": event_type,
        "userId":      user_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        **fields,
    }))


# ── Public entry point ─────────────────────────────────────────────────────────

def archive_user(user_id, dynamodb, s3_client):
    """
    Full Phase 2 pipeline for one user:
      1. Resolve — collect all rows and S3 keys
      2. Archive  — copy everything to digilux-honeywell-archive
      3. Verify   — gate: confirm archive write succeeded
      4. Delete   — hard-delete source data only after verification passes

    Raises on unrecoverable failure so the caller can log and continue to next user.
    """
    logger.info(f"[{user_id}] Phase 2 started")
    _audit_event("PHASE2_STARTED", user_id)

    # Guard: only process users that are INACTIVE and pending archive
    item = dynamodb.Table(TABLE_USER_DATA).get_item(Key={"userId": user_id}).get("Item")
    if not item:
        raise ValueError(f"userId={user_id} not found in user_data")
    if item.get("status") != "INACTIVE":
        raise ValueError(f"userId={user_id} is not INACTIVE (status={item.get('status')!r})")
    if not item.get("archivePending"):
        raise ValueError(f"userId={user_id} archivePending is not True — already processed?")

    logger.info(f"[{user_id}] Step 1 — resolving all user data")
    resolved = _resolve(user_id, dynamodb, s3_client)
    resolve_done_at = datetime.now(timezone.utc).isoformat()
    _audit_event("RESOLVE_COMPLETE", user_id,
                 devices=len(resolved["device_data"]),
                 scenes=len(resolved["scene_data"]),
                 automationEvents=len(resolved["automation_event"]),
                 schedulesDirect=len(resolved["automation_schedule_direct"]),
                 schedulesCtrl=len(resolved["automation_schedule_ctrl"]),
                 s3Objects=len(resolved["s3_keys"]))

    logger.info(f"[{user_id}] Step 2 — archiving to s3://{ARCHIVE_BUCKET}/archive/{user_id}/")
    _archive(user_id, resolved, s3_client)
    archive_done_at = datetime.now(timezone.utc).isoformat()
    _audit_event("ARCHIVE_COMPLETE", user_id,
                 dynamodbCollections=14,
                 s3ObjectsCopied=len(resolved["s3_keys"]))

    logger.info(f"[{user_id}] Step 3 — verifying archive integrity")
    _verify(user_id, resolved, s3_client)       # raises VerificationError if archive incomplete
    verify_done_at = datetime.now(timezone.utc).isoformat()
    _audit_event("VERIFY_COMPLETE", user_id, filesVerified=14)

    logger.info(f"[{user_id}] Step 4 — hard-deleting source data")
    _hard_delete(user_id, resolved, dynamodb, s3_client,
                 resolve_done_at=resolve_done_at,
                 archive_done_at=archive_done_at,
                 verify_done_at=verify_done_at)

    _audit_event("PHASE2_COMPLETE", user_id)
    logger.info(f"[{user_id}] Phase 2 complete")


# ── Step 1: Resolve ────────────────────────────────────────────────────────────

def _resolve(user_id, dynamodb, s3_client):
    """Collect every row and S3 key that belongs to this user. Nothing is deleted here."""
    r = {}

    # Direct by userId
    r["device_data"]          = _query_all(dynamodb, TABLE_DEVICE_DATA,         "userId",     user_id, index="userId-index")
    r["scene_data"]           = _query_all(dynamodb, TABLE_SCENE_DATA,           "userId",     user_id, index="userId-index")
    r["user_device_details"]  = _query_all(dynamodb, TABLE_USER_DEVICE_DETAILS,  "userId",     user_id)
    r["user_device_mapping"]  = _query_all(dynamodb, TABLE_USER_DEVICE_MAPPING,  "userId",     user_id)
    r["user_subuser_detail"]  = _query_all(dynamodb, TABLE_USER_SUBUSER_DETAIL,  "userId",     user_id)
    r["user_subuser_mapping"] = _query_all(dynamodb, TABLE_USER_SUBUSER_MAPPING, "mainUserId", user_id, index="GSI_MainUser")
    r["subuser_role_data"]    = _query_all(dynamodb, TABLE_SUBUSER_ROLE_DATA,    "mainUserId", user_id)
    r["admin_otp_data"]       = _query_all(dynamodb, TABLE_ADMIN_OTP_DATA,       "userId",     user_id)
    r["alexa_lwa_tokens"]     = _query_all(dynamodb, TABLE_ALEXA_LWA_TOKENS,     "userId",     user_id)

    logger.info(
        f"[{user_id}] Direct-resolve counts — "
        f"devices={len(r['device_data'])} scenes={len(r['scene_data'])} "
        f"user_device_details={len(r['user_device_details'])} "
        f"user_device_mapping={len(r['user_device_mapping'])} "
        f"user_subuser_detail={len(r['user_subuser_detail'])} "
        f"user_subuser_mapping={len(r['user_subuser_mapping'])} "
        f"subuser_role_data={len(r['subuser_role_data'])} "
        f"admin_otp_data={len(r['admin_otp_data'])} "
        f"alexa_lwa_tokens={len(r['alexa_lwa_tokens'])}"
    )

    # Cascade: collect deviceIds and uniqueSceneIds for secondary lookups
    device_ids       = [d["deviceId"]       for d in r["device_data"]]
    unique_scene_ids = [s["uniqueSceneId"]  for s in r["scene_data"]]
    logger.info(f"[{user_id}] Cascade inputs — device_ids={device_ids} unique_scene_ids={unique_scene_ids}")

    r["device_state"]             = _resolve_device_state(dynamodb, device_ids)
    r["entity_state"]             = _resolve_entity_state(dynamodb, device_ids)
    r["automation_event"]         = []
    r["automation_schedule_direct"] = []
    r["automation_schedule_ctrl"] = []
    _resolve_automations(dynamodb, unique_scene_ids, device_ids, r)

    logger.info(
        f"[{user_id}] Cascade-resolve counts — "
        f"device_state={len(r['device_state'])} entity_state={len(r['entity_state'])} "
        f"automation_event={len(r['automation_event'])} "
        f"automation_schedule_direct={len(r['automation_schedule_direct'])} "
        f"automation_schedule_ctrl={len(r['automation_schedule_ctrl'])}"
    )

    # S3 objects under the user's metadata folder
    r["s3_keys"] = _list_s3_prefix(s3_client, METADATA_BUCKET, f"{user_id}/")

    logger.info(
        f"[{user_id}] Resolved — "
        f"devices={len(r['device_data'])} scenes={len(r['scene_data'])} "
        f"automation_event={len(r['automation_event'])} "
        f"schedule_direct={len(r['automation_schedule_direct'])} "
        f"schedule_ctrl={len(r['automation_schedule_ctrl'])} "
        f"s3_objects={len(r['s3_keys'])}"
    )
    return r


def _resolve_device_state(dynamodb, device_ids):
    rows = []
    for device_id in device_ids:
        try:
            item = dynamodb.Table(TABLE_DEVICE_STATE).get_item(Key={"deviceId": device_id}).get("Item")
            if item:
                rows.append(item)
                logger.debug(f"device_state resolved for deviceId={device_id}")
            else:
                logger.debug(f"device_state not found for deviceId={device_id} — skipping")
        except Exception:
            logger.exception(f"Failed to resolve device_state for deviceId={device_id}")
    logger.info(f"device_state resolved — {len(rows)} of {len(device_ids)} devices had state records")
    return rows


def _resolve_entity_state(dynamodb, device_ids):
    """
    entity_state has no GSI — scan with FilterExpression per device.
    TODO: Confirm table key schema with team. If PK=deviceId replace scan with get_item/query.
    """
    rows = []
    table = dynamodb.Table(TABLE_ENTITY_STATE)
    for device_id in device_ids:
        try:
            resp  = table.scan(FilterExpression=Attr("deviceId").eq(device_id))
            found = resp.get("Items", [])
            rows.extend(found)
            logger.debug(f"entity_state resolved for deviceId={device_id} — {len(found)} rows")
        except Exception:
            logger.exception(f"Failed to resolve entity_state for deviceId={device_id}")
    logger.info(f"entity_state resolved — {len(rows)} total rows across {len(device_ids)} devices")
    return rows


def _resolve_automations(dynamodb, unique_scene_ids, device_ids, r):
    """
    Populate r["automation_event"], r["automation_schedule_direct"], r["automation_schedule_ctrl"].
    Deduplicates automation_event rows collected via both sceneId and duid paths.
    """
    seen_automation_ids = set()

    # By uniqueSceneId via GSI_scene_automate
    for unique_scene_id in unique_scene_ids:
        for table_key, table_name in (
            ("automation_schedule_direct", TABLE_AUTOMATION_SCHEDULE_DIRECT),
            ("automation_schedule_ctrl",   TABLE_AUTOMATION_SCHEDULE_CTRL),
        ):
            try:
                rows = _query_all(dynamodb, table_name, "sceneId", unique_scene_id, index="GSI_scene_automate")
                r[table_key].extend(rows)
                logger.debug(f"{table_name} — {len(rows)} rows for sceneId={unique_scene_id}")
            except Exception:
                logger.exception(f"Failed to resolve {table_name} for sceneId={unique_scene_id}")

        try:
            rows = _query_all(dynamodb, TABLE_AUTOMATION_EVENT, "sceneId", unique_scene_id, index="GSI_scene_automate")
            new_rows = 0
            for row in rows:
                if row["automationId"] not in seen_automation_ids:
                    r["automation_event"].append(row)
                    seen_automation_ids.add(row["automationId"])
                    new_rows += 1
            logger.debug(f"automation_event via sceneId={unique_scene_id} — {new_rows} new rows ({len(rows)-new_rows} duplicates skipped)")
        except Exception:
            logger.exception(f"Failed to resolve automation_event for sceneId={unique_scene_id}")

    # By duid via GSI_DuidEndpoint (catches device-triggered automations not linked to a scene)
    for device_id in device_ids:
        try:
            rows = _query_all(dynamodb, TABLE_AUTOMATION_EVENT, "duid", device_id, index="GSI_DuidEndpoint")
            new_rows = 0
            for row in rows:
                if row["automationId"] not in seen_automation_ids:
                    r["automation_event"].append(row)
                    seen_automation_ids.add(row["automationId"])
                    new_rows += 1
            logger.debug(f"automation_event via duid={device_id} — {new_rows} new rows ({len(rows)-new_rows} duplicates skipped)")
        except Exception:
            logger.exception(f"Failed to resolve automation_event for duid={device_id}")

    logger.info(
        f"Automation resolve complete — "
        f"automation_event={len(r['automation_event'])} "
        f"schedule_direct={len(r['automation_schedule_direct'])} "
        f"schedule_ctrl={len(r['automation_schedule_ctrl'])} "
        f"unique_automation_ids={len(seen_automation_ids)}"
    )


def _list_s3_prefix(s3_client, bucket, prefix):
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# ── Step 2: Archive ────────────────────────────────────────────────────────────

def _archive(user_id, r, s3_client):
    """Write all resolved data to digilux-honeywell-archive/archive/{userId}/."""
    prefix = f"archive/{user_id}/"

    # Write each logical collection as a JSON file
    collections = {
        "device_data":                r["device_data"],
        "scene_data":                 r["scene_data"],
        "user_device_details":        r["user_device_details"],
        "user_device_mapping":        r["user_device_mapping"],
        "user_subuser_detail":        r["user_subuser_detail"],
        "user_subuser_mapping":       r["user_subuser_mapping"],
        "subuser_role_data":          r["subuser_role_data"],
        "admin_otp_data":             r["admin_otp_data"],
        "alexa_lwa_tokens":           r["alexa_lwa_tokens"],
        "device_state":               r["device_state"],
        "entity_state":               r["entity_state"],
        "automation_event":           r["automation_event"],
        "automation_schedule_direct": r["automation_schedule_direct"],
        "automation_schedule_ctrl":   r["automation_schedule_ctrl"],
    }

    for name, items in collections.items():
        key = f"{prefix}dynamodb/{name}.json"
        s3_client.put_object(
            Bucket=ARCHIVE_BUCKET,
            Key=key,
            Body=json.dumps(items, default=str),
            ContentType="application/json",
        )
        logger.info(f"[{user_id}] Archived {len(items)} rows → {key}")

    # Copy S3 metadata objects into archive bucket
    for src_key in r["s3_keys"]:
        dest_key = f"{prefix}metadata/{src_key}"
        try:
            s3_client.copy_object(
                CopySource={"Bucket": METADATA_BUCKET, "Key": src_key},
                Bucket=ARCHIVE_BUCKET,
                Key=dest_key,
            )
        except Exception:
            logger.exception(f"[{user_id}] S3 copy failed for {src_key}")
            raise RuntimeError(f"Archive failed: could not copy S3 object {src_key}")

    logger.info(f"[{user_id}] Archive done — {len(r['s3_keys'])} S3 objects copied")


# ── Step 3: Verify ─────────────────────────────────────────────────────────────

def _verify(user_id, r, s3_client):
    """
    Confirm every DynamoDB archive JSON exists and is non-empty in the archive bucket.
    Raises VerificationError if anything is missing — hard-delete will NOT proceed.
    """
    prefix = f"archive/{user_id}/dynamodb/"
    expected = [
        "device_data", "scene_data", "user_device_details", "user_device_mapping",
        "user_subuser_detail", "user_subuser_mapping", "subuser_role_data",
        "admin_otp_data", "alexa_lwa_tokens", "device_state", "entity_state",
        "automation_event", "automation_schedule_direct", "automation_schedule_ctrl",
    ]

    for name in expected:
        key = f"{prefix}{name}.json"
        logger.debug(f"[{user_id}] Verifying {key}")
        try:
            head = s3_client.head_object(Bucket=ARCHIVE_BUCKET, Key=key)
            size = head["ContentLength"]
            if size == 0:
                raise VerificationError(f"Archive file is empty: {key}")
            logger.debug(f"[{user_id}] Verified {key} — {size} bytes")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                raise VerificationError(f"Archive file missing: {key}")
            raise

    logger.info(f"[{user_id}] Verification passed — all {len(expected)} archive files present and non-empty")


class VerificationError(Exception):
    pass


# ── Step 4: Hard delete ────────────────────────────────────────────────────────

def _hard_delete(user_id, r, dynamodb, s3_client, *,
                 resolve_done_at=None, archive_done_at=None, verify_done_at=None):
    """
    Delete all resolved rows and S3 objects from source.
    Order: cascade tables first, then direct-userId tables, then S3, then mark done.
    Individual failures are collected and logged — they do not abort the run.
    """
    errors        = []
    tables_deleted = {}   # table_name → count of rows successfully deleted

    # ── Cascade: automation_event ──────────────────────────────────────────────
    _deleted = 0
    for row in r["automation_event"]:
        try:
            dynamodb.Table(TABLE_AUTOMATION_EVENT).delete_item(
                Key={"automationId": row["automationId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] automation_event delete failed for {row.get('automationId')}")
            errors.append("automation_event")
    tables_deleted["automation_event"] = _deleted
    logger.info(f"[{user_id}] automation_event — deleted {_deleted}/{len(r['automation_event'])}")

    # ── Cascade: automation_schedule_direct ────────────────────────────────────
    _deleted = 0
    for row in r["automation_schedule_direct"]:
        try:
            dynamodb.Table(TABLE_AUTOMATION_SCHEDULE_DIRECT).delete_item(
                Key={"uuid": row["uuid"], "automationId": row["automationId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] automation_schedule_direct delete failed")
            errors.append("automation_schedule_direct")
    tables_deleted["automation_schedule_direct"] = _deleted
    logger.info(f"[{user_id}] automation_schedule_direct — deleted {_deleted}/{len(r['automation_schedule_direct'])}")

    # ── Cascade: automation_schedule_ctrl ──────────────────────────────────────
    _deleted = 0
    for row in r["automation_schedule_ctrl"]:
        try:
            dynamodb.Table(TABLE_AUTOMATION_SCHEDULE_CTRL).delete_item(
                Key={"uuid": row["uuid"], "automationId": row["automationId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] automation_schedule_ctrl delete failed")
            errors.append("automation_schedule_ctrl")
    tables_deleted["automation_schedule_ctrl"] = _deleted
    logger.info(f"[{user_id}] automation_schedule_ctrl — deleted {_deleted}/{len(r['automation_schedule_ctrl'])}")

    # ── Cascade: device_state ──────────────────────────────────────────────────
    _deleted = 0
    for row in r["device_state"]:
        try:
            dynamodb.Table(TABLE_DEVICE_STATE).delete_item(Key={"deviceId": row["deviceId"]})
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] device_state delete failed for {row.get('deviceId')}")
            errors.append("device_state")
    tables_deleted["device_state"] = _deleted
    logger.info(f"[{user_id}] device_state — deleted {_deleted}/{len(r['device_state'])}")

    # ── Cascade: entity_state ──────────────────────────────────────────────────
    # TODO: Confirm composite key with team (assumed deviceId PK + endpointType SK).
    _deleted = 0
    for row in r["entity_state"]:
        try:
            dynamodb.Table(TABLE_ENTITY_STATE).delete_item(
                Key={"deviceId": row["deviceId"], "endpointType": row["endpointType"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] entity_state delete failed")
            errors.append("entity_state")
    tables_deleted["entity_state"] = _deleted
    logger.info(f"[{user_id}] entity_state — deleted {_deleted}/{len(r['entity_state'])}")

    # ── Direct: scene_data ─────────────────────────────────────────────────────
    _deleted = 0
    for row in r["scene_data"]:
        try:
            dynamodb.Table(TABLE_SCENE_DATA).delete_item(Key={"uniqueSceneId": row["uniqueSceneId"]})
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] scene_data delete failed for {row.get('uniqueSceneId')}")
            errors.append("scene_data")
    tables_deleted["scene_data"] = _deleted
    logger.info(f"[{user_id}] scene_data — deleted {_deleted}/{len(r['scene_data'])}")

    # ── Direct: device_data ────────────────────────────────────────────────────
    _deleted = 0
    for row in r["device_data"]:
        try:
            dynamodb.Table(TABLE_DEVICE_DATA).delete_item(
                Key={"deviceId": row["deviceId"], "macAddress": row["macAddress"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] device_data delete failed for {row.get('deviceId')}")
            errors.append("device_data")
    tables_deleted["device_data"] = _deleted
    logger.info(f"[{user_id}] device_data — deleted {_deleted}/{len(r['device_data'])}")

    # ── Direct: user_device_details ────────────────────────────────────────────
    _deleted = 0
    for row in r["user_device_details"]:
        try:
            dynamodb.Table(TABLE_USER_DEVICE_DETAILS).delete_item(
                Key={"userId": user_id, "siteId": row["siteId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] user_device_details delete failed")
            errors.append("user_device_details")
    tables_deleted["user_device_details"] = _deleted
    logger.info(f"[{user_id}] user_device_details — deleted {_deleted}/{len(r['user_device_details'])}")

    # ── Direct: user_device_mapping ────────────────────────────────────────────
    _deleted = 0
    for row in r["user_device_mapping"]:
        try:
            dynamodb.Table(TABLE_USER_DEVICE_MAPPING).delete_item(
                Key={"userId": user_id, "siteId": row["siteId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] user_device_mapping delete failed")
            errors.append("user_device_mapping")
    tables_deleted["user_device_mapping"] = _deleted
    logger.info(f"[{user_id}] user_device_mapping — deleted {_deleted}/{len(r['user_device_mapping'])}")

    # ── Direct: user_subuser_detail ────────────────────────────────────────────
    _deleted = 0
    for row in r["user_subuser_detail"]:
        try:
            dynamodb.Table(TABLE_USER_SUBUSER_DETAIL).delete_item(
                Key={"userId": user_id, "subUserId": row["subUserId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] user_subuser_detail delete failed")
            errors.append("user_subuser_detail")
    tables_deleted["user_subuser_detail"] = _deleted
    logger.info(f"[{user_id}] user_subuser_detail — deleted {_deleted}/{len(r['user_subuser_detail'])}")

    # ── Direct: user_subuser_mapping ───────────────────────────────────────────
    _deleted = 0
    for row in r["user_subuser_mapping"]:
        try:
            dynamodb.Table(TABLE_USER_SUBUSER_MAPPING).delete_item(
                Key={"subuserId": row["subuserId"], "requestId": row["requestId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] user_subuser_mapping delete failed")
            errors.append("user_subuser_mapping")
    tables_deleted["user_subuser_mapping"] = _deleted
    logger.info(f"[{user_id}] user_subuser_mapping — deleted {_deleted}/{len(r['user_subuser_mapping'])}")

    # ── Direct: subuser_role_data ──────────────────────────────────────────────
    _deleted = 0
    for row in r["subuser_role_data"]:
        try:
            dynamodb.Table(TABLE_SUBUSER_ROLE_DATA).delete_item(
                Key={"mainUserId": user_id, "roleId": row["roleId"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] subuser_role_data delete failed")
            errors.append("subuser_role_data")
    tables_deleted["subuser_role_data"] = _deleted
    logger.info(f"[{user_id}] subuser_role_data — deleted {_deleted}/{len(r['subuser_role_data'])}")

    # ── Direct: admin_otp_data ─────────────────────────────────────────────────
    _deleted = 0
    for row in r["admin_otp_data"]:
        try:
            dynamodb.Table(TABLE_ADMIN_OTP_DATA).delete_item(
                Key={"userId": user_id, "moduleCategory": row["moduleCategory"]}
            )
            _deleted += 1
        except Exception:
            logger.exception(f"[{user_id}] admin_otp_data delete failed")
            errors.append("admin_otp_data")
    tables_deleted["admin_otp_data"] = _deleted
    logger.info(f"[{user_id}] admin_otp_data — deleted {_deleted}/{len(r['admin_otp_data'])}")

    # ── Direct: alexa_lwa_tokens ───────────────────────────────────────────────
    try:
        dynamodb.Table(TABLE_ALEXA_LWA_TOKENS).delete_item(Key={"userId": user_id})
        tables_deleted["alexa_lwa_tokens"] = 1
        logger.info(f"[{user_id}] alexa_lwa_tokens — deleted 1/1")
    except Exception:
        tables_deleted["alexa_lwa_tokens"] = 0
        logger.exception(f"[{user_id}] alexa_lwa_tokens delete failed")
        errors.append("alexa_lwa_tokens")

    # ── S3: delete metadata objects in batches of 1000 ────────────────────────
    if r["s3_keys"]:
        try:
            for i in range(0, len(r["s3_keys"]), 1000):
                batch = [{"Key": k} for k in r["s3_keys"][i:i + 1000]]
                s3_client.delete_objects(Bucket=METADATA_BUCKET, Delete={"Objects": batch})
            logger.info(f"[{user_id}] Deleted {len(r['s3_keys'])} S3 objects from metadata bucket")
        except Exception:
            logger.exception(f"[{user_id}] S3 metadata delete failed")
            errors.append("s3_metadata")

    # ── Mark archivePending=False in user_data ─────────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamodb.Table(TABLE_USER_DATA).update_item(
            Key={"userId": user_id},
            UpdateExpression="SET archivePending = :false, archivedAt = :ts",
            ExpressionAttributeValues={":false": False, ":ts": now},
        )
    except Exception:
        logger.exception(f"[{user_id}] Failed to clear archivePending on user_data")
        errors.append("clear_archive_pending")

    # ── Update deletion_audit — comprehensive record of every step ─────────────
    final_status = "PHASE2_PARTIAL" if errors else "PHASE2_COMPLETE"
    try:
        requested_at = _get_audit_sk(dynamodb, user_id)
        dynamodb.Table(TABLE_DELETION_AUDIT).update_item(
            Key={"userId": user_id, "requestedAt": requested_at},
            UpdateExpression=(
                "SET #s = :s, phase2CompletedAt = :ts, errors = :e, "
                "resolveCompletedAt = :rd, archiveCompletedAt = :ad, "
                "verifyCompletedAt = :vd, hardDeleteCompletedAt = :hd, "
                "tablesDeleted = :td, s3ObjectsDeleted = :s3"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s":  final_status,
                ":ts": now,
                ":e":  errors,
                ":rd": resolve_done_at  or "unknown",
                ":ad": archive_done_at  or "unknown",
                ":vd": verify_done_at   or "unknown",
                ":hd": now,
                ":td": tables_deleted,
                ":s3": len(r["s3_keys"]),
            },
        )
        logger.info(f"[{user_id}] Audit log updated — status={final_status}")
    except Exception:
        logger.exception(f"[{user_id}] Audit log update failed — non-fatal")

    _audit_event(
        "HARD_DELETE_COMPLETE" if not errors else "HARD_DELETE_PARTIAL",
        user_id,
        status=final_status,
        errors=errors,
        tablesDeleted=tables_deleted,
        s3ObjectsDeleted=len(r["s3_keys"]),
    )

    if errors:
        logger.warning(f"[{user_id}] Hard-delete finished with partial errors: {errors}")
    else:
        logger.info(f"[{user_id}] Hard-delete complete — all source data removed")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _query_all(dynamodb, table_name, key_attr, key_val, index=None):
    """Query a DynamoDB table (or GSI) by a single key attribute, handles pagination."""
    table  = dynamodb.Table(table_name)
    kwargs = {"KeyConditionExpression": Key(key_attr).eq(key_val)}
    if index:
        kwargs["IndexName"] = index

    items, page = [], 0
    while True:
        resp       = table.query(**kwargs)
        page_items = resp.get("Items", [])
        items.extend(page_items)
        page += 1
        lek = resp.get("LastEvaluatedKey")
        logger.debug(
            f"_query_all {table_name} page={page} "
            f"this_page={len(page_items)} total={len(items)} has_more={bool(lek)}"
        )
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _get_audit_sk(dynamodb, user_id):
    """Fetch the requestedAt sort key for this user's audit row."""
    try:
        resp = dynamodb.Table(TABLE_DELETION_AUDIT).query(
            KeyConditionExpression=Key("userId").eq(user_id),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0]["requestedAt"] if items else "unknown"
    except Exception:
        return "unknown"
