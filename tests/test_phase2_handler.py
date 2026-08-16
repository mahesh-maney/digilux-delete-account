"""
Phase 2 Lambda Handler Tests
Covers: EventBridge daily sweep (pagination, failures, empty) and Force-Archive admin API.
"""

import sys
import os
import json
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

PHASE2_DIR = os.path.join(os.path.dirname(__file__), '..', 'phase2')
sys.path.insert(0, PHASE2_DIR)

# Load with a unique module name to avoid sys.modules collision with test_phase1
def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "phase2_lambda_function",
        os.path.join(PHASE2_DIR, "lambda_function.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["phase2_lambda_function"] = mod
    with patch('boto3.resource'), patch('boto3.client'):
        spec.loader.exec_module(mod)
    return mod

handler = _load_handler()

USER_ID = "cognito-sub-uuid-abc-123"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _force_event(user_id=USER_ID):
    return {"body": json.dumps({"userId": user_id})}


def _sweep_event(source="aws.scheduler"):
    return {"source": source}


def _db_with_users(users, paginated=False):
    mock_db = MagicMock()
    if paginated:
        mock_db.Table.return_value.scan.side_effect = users  # list of scan page dicts
    else:
        mock_db.Table.return_value.scan.return_value = {"Items": users}
    return mock_db


# ── Force-Archive admin API ────────────────────────────────────────────────────

class TestForceArchive(unittest.TestCase):

    def test_happy_path_returns_200(self):
        with patch('phase2_lambda_function.archive_user') as mock_archive:
            resp = handler.lambda_handler(_force_event(), None)
        self.assertEqual(resp["statusCode"], 200)
        mock_archive.assert_called_once_with(USER_ID, handler.dynamodb, handler.s3_client)

    def test_success_message_includes_user_id(self):
        with patch('phase2_lambda_function.archive_user'):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertIn(USER_ID, json.loads(resp["body"])["message"])

    def test_missing_user_id_returns_400(self):
        resp = handler.lambda_handler({"body": json.dumps({})}, None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("userId", json.loads(resp["body"])["error"])

    def test_whitespace_only_user_id_returns_400(self):
        resp = handler.lambda_handler({"body": json.dumps({"userId": "   "})}, None)
        self.assertEqual(resp["statusCode"], 400)

    def test_missing_body_returns_400(self):
        resp = handler.lambda_handler({}, None)
        self.assertEqual(resp["statusCode"], 400)

    def test_empty_body_string_returns_400(self):
        resp = handler.lambda_handler({"body": ""}, None)
        self.assertEqual(resp["statusCode"], 400)

    def test_verification_error_returns_500_with_clear_message(self):
        from archiver import VerificationError
        with patch('phase2_lambda_function.archive_user',
                   side_effect=VerificationError("device_data.json missing")):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertEqual(resp["statusCode"], 500)
        self.assertIn("verification", json.loads(resp["body"])["error"].lower())

    def test_verification_error_response_mentions_aborted(self):
        """Response must signal that hard-delete was aborted — not silently dropped."""
        from archiver import VerificationError
        with patch('phase2_lambda_function.archive_user',
                   side_effect=VerificationError("missing")):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertIn("aborted", json.loads(resp["body"])["error"].lower())

    def test_value_error_returns_400(self):
        with patch('phase2_lambda_function.archive_user',
                   side_effect=ValueError("userId not found in user_data")):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_unexpected_exception_returns_500(self):
        with patch('phase2_lambda_function.archive_user',
                   side_effect=Exception("Unexpected DynamoDB error")):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertEqual(resp["statusCode"], 500)

    def test_response_has_cors_headers(self):
        with patch('phase2_lambda_function.archive_user'):
            resp = handler.lambda_handler(_force_event(), None)
        self.assertIn("Access-Control-Allow-Origin", resp["headers"])


# ── EventBridge daily sweep ────────────────────────────────────────────────────

class TestEventBridgeSweep(unittest.TestCase):

    def test_aws_scheduler_source_triggers_sweep(self):
        mock_db = _db_with_users([])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user'):
            result = handler.lambda_handler(_sweep_event("aws.scheduler"), None)
        self.assertIn("processed", result)

    def test_aws_events_source_also_triggers_sweep(self):
        mock_db = _db_with_users([])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user'):
            result = handler.lambda_handler(_sweep_event("aws.events"), None)
        self.assertIn("processed", result)

    def test_no_inactive_users_returns_zero_counts(self):
        mock_db = _db_with_users([])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user') as mock_archive:
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["failed"],    0)
        mock_archive.assert_not_called()

    def test_two_users_both_processed(self):
        users = [{"userId": "user-1"}, {"userId": "user-2"}]
        mock_db = _db_with_users(users)
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user') as mock_archive:
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["failed"],    0)
        self.assertEqual(mock_archive.call_count, 2)

    def test_single_failure_counted_as_failed(self):
        mock_db = _db_with_users([{"userId": "user-1"}])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user', side_effect=Exception("DB down")):
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["failed"],    1)

    def test_one_failure_does_not_stop_other_users(self):
        from archiver import VerificationError
        users = [{"userId": "user-1"}, {"userId": "user-2"}, {"userId": "user-3"}]
        mock_db = _db_with_users(users)

        def side_effect(uid, *args):
            if uid == "user-2":
                raise VerificationError("Archive write failed")

        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user', side_effect=side_effect):
            result = handler.lambda_handler(_sweep_event(), None)

        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["failed"],    1)

    def test_verification_error_counted_as_failed(self):
        from archiver import VerificationError
        mock_db = _db_with_users([{"userId": "user-1"}])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user',
                   side_effect=VerificationError("missing file")):
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["failed"],    1)
        self.assertEqual(result["processed"], 0)

    def test_paginated_scan_processes_all_pages(self):
        """Sweep must follow LastEvaluatedKey and process users across pages."""
        pages = [
            {"Items": [{"userId": "user-1"}], "LastEvaluatedKey": {"userId": "user-1"}},
            {"Items": [{"userId": "user-2"}]},
        ]
        mock_db = _db_with_users(pages, paginated=True)
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user') as mock_archive:
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"],     2)
        self.assertEqual(mock_archive.call_count, 2)

    def test_sweep_scan_applies_inactive_filter(self):
        """Scan must include a FilterExpression to target only INACTIVE users."""
        mock_db = _db_with_users([])
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user'):
            handler.lambda_handler(_sweep_event(), None)
        scan_kwargs = mock_db.Table.return_value.scan.call_args.kwargs
        self.assertIn("FilterExpression", scan_kwargs)


class TestEdgeCases(unittest.TestCase):

    def test_invalid_json_body_returns_400(self):
        """Malformed JSON in body must not crash the handler."""
        resp = handler.lambda_handler({"body": "not-json-at-all{"}, None)
        self.assertIn(resp["statusCode"], [400, 500])
        # Must return a structured JSON response, not a raw exception
        self.assertIsNotNone(json.loads(resp["body"]))

    def test_three_page_sweep_processes_all_users(self):
        """Sweep must process users across 3+ scan pages."""
        pages = [
            {"Items": [{"userId": "user-1"}], "LastEvaluatedKey": {"userId": "user-1"}},
            {"Items": [{"userId": "user-2"}], "LastEvaluatedKey": {"userId": "user-2"}},
            {"Items": [{"userId": "user-3"}]},
        ]
        mock_db = _db_with_users(pages, paginated=True)
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user') as mock_archive:
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"],     3)
        self.assertEqual(mock_archive.call_count, 3)

    def test_unknown_source_treated_as_force_archive(self):
        """Any event without a known source must fall through to force-archive path."""
        resp = handler.lambda_handler({"source": "custom.trigger", "body": json.dumps({})}, None)
        # Falls through to force-archive → missing userId → 400
        self.assertEqual(resp["statusCode"], 400)

    def test_all_users_fail_in_sweep_returns_correct_counts(self):
        users = [{"userId": f"user-{i}"} for i in range(5)]
        mock_db = _db_with_users(users)
        with patch.object(handler, 'dynamodb', mock_db), \
             patch('phase2_lambda_function.archive_user', side_effect=Exception("DB down")):
            result = handler.lambda_handler(_sweep_event(), None)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["failed"],    5)


if __name__ == "__main__":
    unittest.main()
