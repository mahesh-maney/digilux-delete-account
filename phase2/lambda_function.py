import json
import os
import boto3
import logging

from boto3.dynamodb.conditions import Attr
from archiver import archive_user, restore_user, VerificationError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients ────────────────────────────────────────────────────────────────
dynamodb       = boto3.resource("dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None)
s3_client      = boto3.client("s3")
cognito_client = boto3.client("cognito-idp")

# ── Environment ────────────────────────────────────────────────────────────────
TABLE_USER_DATA = os.environ.get("TABLE_USER_DATA", "digilux_honeywell_user_data")

logger.info(
    "phase2 handler config loaded — "
    f"TABLE_USER_DATA={TABLE_USER_DATA} "
    f"DYNAMODB_ENDPOINT_URL={os.environ.get('DYNAMODB_ENDPOINT_URL', 'default')}"
)


# ── Entry point ────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    source = event.get("source", "")
    method = (event.get("httpMethod") or "").upper()
    path   = event.get("path") or ""

    logger.info(f"phase2_archive_worker invoked — source={source!r} method={method!r} path={path!r}")

    # EventBridge Scheduler — daily INACTIVE sweep
    if source in ("aws.scheduler", "aws.events"):
        return _handle_sweep()

    # Restore endpoint — POST /account/restore
    if method == "POST" and "restore" in path:
        return _handle_restore(event)

    # Force-Archive API — admin POST with explicit userId
    return _handle_force_archive(event)


# ── Trigger: EventBridge daily sweep ──────────────────────────────────────────

def _handle_sweep():
    """
    Scan user_data for all users where status=INACTIVE and archivePending=True,
    then run the full Phase 2 archive pipeline for each.
    """
    logger.info("EventBridge sweep started")

    table  = dynamodb.Table(TABLE_USER_DATA)
    kwargs = {
        "FilterExpression": (
            Attr("status").eq("INACTIVE") & Attr("archivePending").eq(True)
        )
    }

    processed, failed, page = 0, 0, 0

    while True:
        resp  = table.scan(**kwargs)
        users = resp.get("Items", [])
        page += 1
        logger.info(f"Sweep page={page} found={len(users)} pending-archive users")

        for item in users:
            user_id = item["userId"]
            logger.info(f"[{user_id}] Sweep: starting archive")
            try:
                archive_user(user_id, dynamodb, s3_client, cognito_client=cognito_client)
                processed += 1
                logger.info(f"[{user_id}] Sweep: archive complete — running_total processed={processed}")
            except VerificationError as e:
                logger.error(f"[{user_id}] Sweep: verification failed — hard-delete skipped. {e}")
                failed += 1
            except Exception:
                logger.exception(f"[{user_id}] Sweep: archive failed")
                failed += 1

        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    logger.info(f"Sweep complete — pages={page} processed={processed} failed={failed}")
    return {"processed": processed, "failed": failed}


# ── Trigger: Force-Archive admin API ──────────────────────────────────────────

def _handle_force_archive(event):
    """
    Admin-triggered archive for a specific userId.
    Expected body: { "userId": "<userId>" }
    This endpoint must be protected by an admin authorizer on API Gateway.
    """
    try:
        body    = json.loads(event.get("body") or "{}")
        user_id = body.get("userId", "").strip()

        if not user_id:
            logger.warning("Force-archive rejected — userId missing from request body")
            return _resp(400, {"error": "userId is required"})

        logger.info(f"[{user_id}] Force-archive requested")
        archive_user(user_id, dynamodb, s3_client, cognito_client=cognito_client)
        logger.info(f"[{user_id}] Force-archive complete")
        return _resp(200, {"message": f"Archive complete for userId={user_id}"})

    except VerificationError as e:
        logger.error(f"Force-archive verification failed — hard-delete aborted: {e}")
        return _resp(500, {"error": f"Archive verification failed — hard-delete aborted. {e}"})

    except ValueError as e:
        logger.warning(f"Force-archive rejected — {e}")
        return _resp(400, {"error": str(e)})

    except Exception as e:
        logger.exception("Force-archive failed with unexpected error")
        return _resp(500, {"error": str(e)})


# ── POST /account/restore ──────────────────────────────────────────────────────

def _handle_restore(event):
    """
    Restore an account within the 7-day window.
    Expected body: { "userId": "<userId>", "deletionToken": "<token>" }
    """
    try:
        body          = json.loads(event.get("body") or "{}")
        user_id       = (body.get("userId") or "").strip()
        deletion_token = (body.get("deletionToken") or "").strip()

        if not user_id or not deletion_token:
            logger.warning(
                f"Restore rejected — missing required fields "
                f"userId={'present' if user_id else 'missing'} "
                f"deletionToken={'present' if deletion_token else 'missing'}"
            )
            return _resp(400, {"error": "userId and deletionToken are required"})

        logger.info(f"[{user_id}] Restore requested — token={deletion_token}")
        restore_user(user_id, deletion_token, dynamodb, s3_client, cognito_client)
        logger.info(f"[{user_id}] Restore complete")
        return _resp(200, {
            "message": "Your account has been successfully restored. You can log in again immediately.",
            "status":  "restored",
        })

    except ValueError as e:
        logger.warning(f"Restore rejected — {e}")
        return _resp(400, {"error": str(e)})

    except RuntimeError as e:
        logger.error(f"Restore failed — {e}")
        return _resp(500, {"error": str(e)})

    except Exception as e:
        logger.exception("Restore failed with unexpected error")
        return _resp(500, {"error": str(e)})


# ── Response helper ────────────────────────────────────────────────────────────

def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Content-Type":                 "application/json",
        },
        "body": json.dumps(body),
    }
