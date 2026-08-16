"""
Phase 1 — Account Deletion Lambda Tests
Covers: auth, idempotency, sign-out, mark-INACTIVE, device release, Cognito delete, audit log.
"""

import sys
import os
import json
import base64
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

PHASE1_DIR = os.path.join(os.path.dirname(__file__), '..', 'phase1')
sys.path.insert(0, PHASE1_DIR)

# Load with a unique module name to avoid sys.modules collision across test files
def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "phase1_lambda_function",
        os.path.join(PHASE1_DIR, "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["phase1_lambda_function"] = mod
    with patch('boto3.resource'), patch('boto3.client'):
        spec.loader.exec_module(mod)
    return mod

handler = _load_handler()

USER_ID = "cognito-sub-uuid-abc-123"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_jwt(sub=USER_ID, **extra):
    claims = {"sub": sub, **extra}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload_b64}.fakesig"


def _event(token=None, sub=USER_ID):
    return {"headers": {"Authorization": f"Bearer {token or _make_jwt(sub=sub)}"}}


def _mock_cognito():
    mock = MagicMock()
    mock.exceptions.UserNotFoundException = type("UserNotFoundException", (Exception,), {})
    return mock


def _mock_db(user_status="ACTIVE"):
    """DynamoDB mock with a user record and sensible defaults for all operations."""
    mock = MagicMock()
    user_item = {"userId": USER_ID, "status": user_status} if user_status else None
    mock.Table.return_value.get_item.return_value = {"Item": user_item}
    mock.Table.return_value.update_item.return_value = {}
    mock.Table.return_value.query.return_value = {"Items": []}
    mock.Table.return_value.put_item.return_value = {}
    return mock


# ── Auth tests ─────────────────────────────────────────────────────────────────

class TestAuth(unittest.TestCase):

    def test_no_headers_returns_401(self):
        resp = handler.lambda_handler({}, None)
        self.assertEqual(resp["statusCode"], 401)
        self.assertIn("Authorization token missing", json.loads(resp["body"])["error"])

    def test_empty_headers_dict_returns_401(self):
        resp = handler.lambda_handler({"headers": {}}, None)
        self.assertEqual(resp["statusCode"], 401)

    def test_jwt_with_only_two_parts_returns_401(self):
        event = {"headers": {"Authorization": "Bearer header.payload"}}
        resp = handler.lambda_handler(event, None)
        self.assertEqual(resp["statusCode"], 401)

    def test_jwt_with_no_sub_claim_returns_401(self):
        claims = {"username": "someone", "cognito:username": "someone"}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        token = f"header.{payload_b64}.sig"
        resp = handler.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, None)
        self.assertEqual(resp["statusCode"], 401)
        self.assertIn("sub", json.loads(resp["body"])["error"])

    def test_lowercase_authorization_header_is_accepted(self):
        with patch.object(handler, 'dynamodb', _mock_db()), \
             patch.object(handler, 'cognito_idp', _mock_cognito()):
            event = {"headers": {"authorization": f"Bearer {_make_jwt()}"}}
            resp = handler.lambda_handler(event, None)
        self.assertEqual(resp["statusCode"], 200)

    def test_sub_is_used_as_user_id_not_username(self):
        """Verify the sub claim is extracted (not username or cognito:username)."""
        token = _make_jwt(sub="real-sub-id", **{"cognito:username": "different-username"})
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            handler.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, None)
        mock_cog.admin_user_global_sign_out.assert_called_once_with(
            UserPoolId=handler.USER_POOL_ID, Username="real-sub-id"
        )


# ── Idempotency tests ──────────────────────────────────────────────────────────

class TestIdempotency(unittest.TestCase):

    def test_already_inactive_returns_200_already_processed(self):
        with patch.object(handler, 'dynamodb', _mock_db(user_status="INACTIVE")), \
             patch.object(handler, 'cognito_idp', _mock_cognito()):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["status"], "already_processed")

    def test_already_inactive_does_not_call_cognito_delete(self):
        mock_cog = _mock_cognito()
        with patch.object(handler, 'dynamodb', _mock_db(user_status="INACTIVE")), \
             patch.object(handler, 'cognito_idp', mock_cog):
            handler.lambda_handler(_event(), None)
        mock_cog.admin_delete_user.assert_not_called()

    def test_idempotency_db_failure_continues_to_deletion(self):
        """If the idempotency get_item fails, Phase 1 should still proceed."""
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.get_item.side_effect = Exception("DynamoDB unavailable")
        mock_db.Table.return_value.update_item.return_value = {}
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)


# ── Happy-path and step tests ──────────────────────────────────────────────────

class TestHappyPath(unittest.TestCase):

    def _run(self, devices=None):
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        if devices is not None:
            mock_db.Table.return_value.query.return_value = {"Items": devices}
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        return resp, mock_db, mock_cog

    def test_returns_200_with_processing_status(self):
        resp, _, _ = self._run()
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["status"], "processing")

    def test_ack_message_mentions_7_business_days(self):
        resp, _, _ = self._run()
        self.assertIn("7 business days", json.loads(resp["body"])["message"])

    def test_global_sign_out_called_with_correct_user(self):
        _, _, mock_cog = self._run()
        mock_cog.admin_user_global_sign_out.assert_called_once_with(
            UserPoolId=handler.USER_POOL_ID, Username=USER_ID
        )

    def test_cognito_delete_called_with_correct_user(self):
        _, _, mock_cog = self._run()
        mock_cog.admin_delete_user.assert_called_once_with(
            UserPoolId=handler.USER_POOL_ID, Username=USER_ID
        )

    def test_user_marked_inactive_with_archive_pending_true(self):
        _, mock_db, _ = self._run()
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        inactive_call = next(
            (c for c in update_calls
             if c.kwargs.get("ExpressionAttributeValues", {}).get(":inactive") == "INACTIVE"),
            None
        )
        self.assertIsNotNone(inactive_call, "Expected update_item to set status=INACTIVE")
        self.assertTrue(inactive_call.kwargs["ExpressionAttributeValues"].get(":true"))

    def test_two_devices_released_with_user_id_zero(self):
        devices = [
            {"deviceId": "dev-1", "macAddress": "AA:BB:CC"},
            {"deviceId": "dev-2", "macAddress": "DD:EE:FF"},
        ]
        _, mock_db, _ = self._run(devices=devices)
        release_calls = [
            c for c in mock_db.Table.return_value.update_item.call_args_list
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":v") == "0"
        ]
        self.assertEqual(len(release_calls), 2)

    def test_user_with_no_devices_completes_successfully(self):
        resp, _, _ = self._run(devices=[])
        self.assertEqual(resp["statusCode"], 200)

    def test_audit_log_written_with_phase1_complete_status(self):
        _, mock_db, _ = self._run()
        put_calls = mock_db.Table.return_value.put_item.call_args_list
        audit_call = next(
            (c for c in put_calls
             if c.kwargs.get("Item", {}).get("status") == "PHASE1_COMPLETE"),
            None
        )
        self.assertIsNotNone(audit_call, "Expected put_item with status=PHASE1_COMPLETE")

    def test_audit_log_contains_user_id_and_timestamp(self):
        _, mock_db, _ = self._run()
        put_calls = mock_db.Table.return_value.put_item.call_args_list
        audit_item = next(
            (c.kwargs["Item"] for c in put_calls if "status" in c.kwargs.get("Item", {})),
            None
        )
        self.assertIsNotNone(audit_item)
        self.assertEqual(audit_item["userId"], USER_ID)
        self.assertIn("requestedAt", audit_item)

    def test_response_includes_cors_headers(self):
        resp, _, _ = self._run()
        self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")


# ── Resilience / negative tests ────────────────────────────────────────────────

class TestResilience(unittest.TestCase):

    def test_mark_inactive_failure_returns_500_and_aborts(self):
        """If the INACTIVE update fails, Phase 1 must abort immediately with 500."""
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.update_item.side_effect = Exception("Write failed")
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 500)
        mock_cog.admin_delete_user.assert_not_called()

    def test_cognito_not_found_on_sign_out_continues(self):
        mock_cog = _mock_cognito()
        mock_cog.admin_user_global_sign_out.side_effect = mock_cog.exceptions.UserNotFoundException()
        with patch.object(handler, 'dynamodb', _mock_db()), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_cognito_not_found_on_delete_continues(self):
        mock_cog = _mock_cognito()
        mock_cog.admin_delete_user.side_effect = mock_cog.exceptions.UserNotFoundException()
        with patch.object(handler, 'dynamodb', _mock_db()), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_device_query_failure_does_not_abort_phase1(self):
        """Device GSI failure is non-fatal — user is still marked INACTIVE."""
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.query.side_effect = Exception("GSI unavailable")
        mock_db.Table.return_value.update_item.return_value = {}
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_audit_log_failure_is_non_fatal(self):
        """Audit log write failure must NOT cause a 500."""
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.put_item.side_effect = Exception("Audit table down")
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_sign_out_general_exception_continues(self):
        """A non-UserNotFoundException during sign-out must not abort Phase 1."""
        mock_cog = _mock_cognito()
        mock_cog.admin_user_global_sign_out.side_effect = Exception("Cognito unavailable")
        with patch.object(handler, 'dynamodb', _mock_db()), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_single_device_release_failure_continues_for_rest(self):
        """If one device release fails, remaining devices should still be attempted."""
        devices = [
            {"deviceId": "dev-1", "macAddress": "AA:BB:CC"},
            {"deviceId": "dev-2", "macAddress": "DD:EE:FF"},
            {"deviceId": "dev-3", "macAddress": "11:22:33"},
        ]
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.query.return_value = {"Items": devices}

        release_attempts = {"n": 0}

        def update_side_effect(**kwargs):
            if kwargs.get("ExpressionAttributeValues", {}).get(":v") == "0":
                release_attempts["n"] += 1
                if release_attempts["n"] == 1:
                    raise Exception("Throttled")
            return {}

        mock_db.Table.return_value.update_item.side_effect = update_side_effect
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(release_attempts["n"], 3)  # all 3 attempted despite first failure


# ── Edge-case / additional negative tests ─────────────────────────────────────

class TestAuthEdgeCases(unittest.TestCase):

    def test_jwt_payload_not_valid_base64_returns_401(self):
        """Corrupted base64 in the payload segment must return 401, not 500."""
        token = "header.!!!not-base64!!$.sig"
        resp  = handler.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, None)
        self.assertEqual(resp["statusCode"], 401)

    def test_jwt_payload_valid_base64_but_not_json_returns_401(self):
        """Valid base64 that decodes to non-JSON must return 401."""
        import base64
        payload_b64 = base64.urlsafe_b64encode(b"this is not json").decode().rstrip("=")
        token = f"header.{payload_b64}.sig"
        resp  = handler.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, None)
        self.assertEqual(resp["statusCode"], 401)

    def test_four_part_jwt_returns_401(self):
        """A JWT with 4 segments (not 3) is invalid and must return 401."""
        token = "a.b.c.d"
        resp  = handler.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, None)
        self.assertEqual(resp["statusCode"], 401)


class TestUserDataAbsent(unittest.TestCase):

    def test_user_not_in_user_data_proceeds_to_deletion(self):
        """
        If get_item returns no Item (user never existed in user_data),
        the idempotency check must not crash — Phase 1 continues.
        """
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.get_item.return_value = {"Item": None}
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)

    def test_user_status_none_proceeds_to_deletion(self):
        """User row exists but status field is absent — not idempotent, should proceed."""
        mock_db  = _mock_db()
        mock_cog = _mock_cognito()
        mock_db.Table.return_value.get_item.return_value = {"Item": {"userId": USER_ID}}
        with patch.object(handler, 'dynamodb', mock_db), \
             patch.object(handler, 'cognito_idp', mock_cog):
            resp = handler.lambda_handler(_event(), None)
        self.assertEqual(resp["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
