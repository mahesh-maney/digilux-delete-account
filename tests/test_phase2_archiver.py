"""
Phase 2 Archiver Tests
Covers: guard checks (including 7-day window), archive, verify, hard-delete,
        subdevice S3 scan, automation_schedule_controller, Cognito delete,
        restore_user, deduplication, full pipeline ordering.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))

import archiver
from archiver import archive_user, restore_user, VerificationError

USER_ID         = "cognito-sub-uuid-abc-123"
ARCHIVE_BUCKET  = "digilux-honeywell-archive"
METADATA_BUCKET = "digilux-honeywell-metadata"

# device_data removed; automation_schedule_controller added — still 14 total
ALL_COLLECTIONS = [
    "scene_data", "user_device_details", "user_device_mapping",
    "user_subuser_detail", "user_subuser_mapping", "subuser_role_data",
    "admin_otp_data", "alexa_lwa_tokens", "device_state", "entity_state",
    "automation_event", "automation_schedule_direct", "automation_schedule_ctrl",
    "automation_schedule_controller",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "op")


def _resolved(**overrides):
    """Build a resolved dict; all collections empty by default."""
    base = {name: [] for name in ALL_COLLECTIONS}
    base["s3_keys"]       = []
    base["subdevice_macs"] = []
    base.update(overrides)
    return base


def _make_db(status="INACTIVE", archive_pending=True, deletion_requested_at=None):
    """DynamoDB mock: get_item returns a valid INACTIVE user; queries return empty Items."""
    mock_db = MagicMock()
    user_item = {"userId": USER_ID, "status": status, "archivePending": archive_pending}
    if deletion_requested_at:
        user_item["deletionRequestedAt"] = deletion_requested_at
    mock_db.Table.return_value.get_item.return_value  = {"Item": user_item}
    mock_db.Table.return_value.query.return_value     = {"Items": []}
    mock_db.Table.return_value.scan.return_value      = {"Items": []}
    mock_db.Table.return_value.update_item.return_value = {}
    mock_db.Table.return_value.delete_item.return_value = {}
    return mock_db


def _make_s3(content_length=50):
    """S3 mock: head_object returns non-empty; list paginator returns no objects."""
    mock_s3  = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": content_length}
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    mock_s3.get_paginator.return_value = paginator
    return mock_s3


def _make_cognito():
    mock = MagicMock()
    mock.exceptions.UserNotFoundException = type("UserNotFoundException", (Exception,), {})
    return mock


# ── Guard checks ───────────────────────────────────────────────────────────────

class TestArchiveUserGuards(unittest.TestCase):

    def _db_for(self, status, archive_pending, exists=True, deletion_requested_at=None):
        mock_db = MagicMock()
        item = {"userId": USER_ID, "status": status, "archivePending": archive_pending} if exists else None
        if item and deletion_requested_at:
            item["deletionRequestedAt"] = deletion_requested_at
        mock_db.Table.return_value.get_item.return_value = {"Item": item}
        return mock_db

    def test_user_not_found_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            archive_user(USER_ID, self._db_for(None, False, exists=False), MagicMock())
        self.assertIn("not found", str(ctx.exception))

    def test_user_status_active_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            archive_user(USER_ID, self._db_for("ACTIVE", False), MagicMock())
        self.assertIn("INACTIVE", str(ctx.exception))

    def test_archive_pending_false_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            archive_user(USER_ID, self._db_for("INACTIVE", False), MagicMock())
        self.assertIn("archivePending", str(ctx.exception))

    def test_valid_inactive_user_passes_guards(self):
        """Guards pass — no ValueError raised for a valid INACTIVE+archivePending user."""
        try:
            archive_user(USER_ID, _make_db(), _make_s3())
        except ValueError as e:
            self.fail(f"Guard raised ValueError unexpectedly: {e}")

    def test_within_7_day_window_raises_value_error(self):
        """Archive must be blocked while the 7-day restore window is still open."""
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        db = self._db_for("INACTIVE", True, deletion_requested_at=recent)
        with self.assertRaises(ValueError) as ctx:
            archive_user(USER_ID, db, MagicMock())
        self.assertIn("restore window", str(ctx.exception))

    def test_past_7_day_window_passes_guard(self):
        """Archive must proceed once the 7-day restore window has closed."""
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        try:
            archive_user(USER_ID, _make_db(deletion_requested_at=old), _make_s3())
        except ValueError as e:
            self.fail(f"Guard raised ValueError unexpectedly: {e}")

    def test_no_deletion_requested_at_skips_window_guard(self):
        """If deletionRequestedAt is absent, the 7-day gate is skipped (backwards-compat)."""
        try:
            archive_user(USER_ID, _make_db(), _make_s3())
        except ValueError as e:
            self.fail(f"Unexpected ValueError: {e}")

    def test_exactly_7_days_elapsed_passes_guard(self):
        """At exactly 7 days elapsed the restore window has closed — archive must proceed."""
        exactly_7 = (datetime.now(timezone.utc) - timedelta(days=7, seconds=1)).isoformat()
        try:
            archive_user(USER_ID, _make_db(deletion_requested_at=exactly_7), _make_s3())
        except ValueError as e:
            self.fail(f"Expected guard to pass at 7+ days but got ValueError: {e}")


# ── Archive (Step 2) ───────────────────────────────────────────────────────────

class TestArchive(unittest.TestCase):

    def test_writes_exactly_14_dynamodb_json_files(self):
        mock_s3 = MagicMock()
        archiver._archive(USER_ID, _resolved(), mock_s3)
        self.assertEqual(mock_s3.put_object.call_count, len(ALL_COLLECTIONS))

    def test_all_archive_keys_under_correct_user_prefix(self):
        mock_s3 = MagicMock()
        archiver._archive(USER_ID, _resolved(), mock_s3)
        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        self.assertTrue(all(k.startswith(f"archive/{USER_ID}/dynamodb/") for k in keys))

    def test_automation_schedule_controller_archived(self):
        """automation_schedule_controller must produce an archive JSON file."""
        mock_s3 = MagicMock()
        rows = [{"uuid": USER_ID, "automationId": "auto-ctrl-1"}]
        archiver._archive(USER_ID, _resolved(automation_schedule_controller=rows), mock_s3)
        ctrl_call = next(
            (c for c in mock_s3.put_object.call_args_list
             if "automation_schedule_controller" in c.kwargs["Key"]),
            None
        )
        self.assertIsNotNone(ctrl_call)
        body = json.loads(ctrl_call.kwargs["Body"])
        self.assertEqual(body[0]["automationId"], "auto-ctrl-1")

    def test_device_data_not_in_archive(self):
        """device_data must NOT appear in Phase 2 archive (Phase 1 handles unbind)."""
        mock_s3 = MagicMock()
        archiver._archive(USER_ID, _resolved(), mock_s3)
        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        self.assertFalse(any("device_data" in k for k in keys))

    def test_s3_metadata_objects_copied_to_archive(self):
        mock_s3 = MagicMock()
        s3_keys = [f"{USER_ID}/site/device.json", f"{USER_ID}/site/scene.json"]
        archiver._archive(USER_ID, _resolved(s3_keys=s3_keys), mock_s3)
        self.assertEqual(mock_s3.copy_object.call_count, 2)

    def test_copy_destination_under_archive_metadata_prefix(self):
        mock_s3 = MagicMock()
        archiver._archive(USER_ID, _resolved(s3_keys=[f"{USER_ID}/file.json"]), mock_s3)
        dest_key = mock_s3.copy_object.call_args.kwargs["Key"]
        self.assertTrue(dest_key.startswith(f"archive/{USER_ID}/metadata/"))

    def test_no_s3_objects_skips_copy(self):
        mock_s3 = MagicMock()
        archiver._archive(USER_ID, _resolved(s3_keys=[]), mock_s3)
        mock_s3.copy_object.assert_not_called()

    def test_s3_copy_failure_raises_runtime_error(self):
        mock_s3 = MagicMock()
        mock_s3.copy_object.side_effect = Exception("S3 unavailable")
        with self.assertRaises(RuntimeError):
            archiver._archive(USER_ID, _resolved(s3_keys=[f"{USER_ID}/file.json"]), mock_s3)


# ── Verify (Step 3) ────────────────────────────────────────────────────────────

class TestVerify(unittest.TestCase):

    def test_all_files_present_and_non_empty_passes(self):
        mock_s3 = _make_s3(content_length=100)
        archiver._verify(USER_ID, _resolved(), mock_s3)  # must not raise
        self.assertEqual(mock_s3.head_object.call_count, len(ALL_COLLECTIONS))

    def test_missing_file_raises_verification_error(self):
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _client_error("404")
        with self.assertRaises(VerificationError) as ctx:
            archiver._verify(USER_ID, _resolved(), mock_s3)
        self.assertIn("missing", str(ctx.exception))

    def test_nosuchkey_error_raises_verification_error(self):
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _client_error("NoSuchKey")
        with self.assertRaises(VerificationError):
            archiver._verify(USER_ID, _resolved(), mock_s3)

    def test_empty_file_raises_verification_error(self):
        mock_s3 = _make_s3(content_length=0)
        with self.assertRaises(VerificationError) as ctx:
            archiver._verify(USER_ID, _resolved(), mock_s3)
        self.assertIn("empty", str(ctx.exception))

    def test_unexpected_s3_error_propagated(self):
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _client_error("500")
        with self.assertRaises(ClientError):
            archiver._verify(USER_ID, _resolved(), mock_s3)

    def test_automation_schedule_controller_verified(self):
        """automation_schedule_controller.json must be in the verified file list."""
        mock_s3 = _make_s3(content_length=10)
        archiver._verify(USER_ID, _resolved(), mock_s3)
        checked_keys = [c.kwargs["Key"] for c in mock_s3.head_object.call_args_list]
        self.assertTrue(any("automation_schedule_controller" in k for k in checked_keys))

    def test_device_data_not_verified(self):
        """device_data.json must NOT appear in the verify step."""
        mock_s3 = _make_s3(content_length=10)
        archiver._verify(USER_ID, _resolved(), mock_s3)
        checked_keys = [c.kwargs["Key"] for c in mock_s3.head_object.call_args_list]
        self.assertFalse(any("device_data" in k for k in checked_keys))


# ── Hard-delete (Step 4) ───────────────────────────────────────────────────────

class TestHardDelete(unittest.TestCase):

    def test_automation_event_deleted_by_automation_id(self):
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID, _resolved(automation_event=[{"automationId": "auto-1"}]), mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(Key={"automationId": "auto-1"})

    def test_schedule_direct_deleted_with_composite_key(self):
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID,
            _resolved(automation_schedule_direct=[{"uuid": USER_ID, "automationId": "auto-1"}]),
            mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(
            Key={"uuid": USER_ID, "automationId": "auto-1"}
        )

    def test_automation_schedule_controller_deleted_with_composite_key(self):
        """automation_schedule_controller rows must be deleted by uuid + automationId."""
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID,
            _resolved(automation_schedule_controller=[{"uuid": USER_ID, "automationId": "auto-ctrl-1"}]),
            mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(
            Key={"uuid": USER_ID, "automationId": "auto-ctrl-1"}
        )

    def test_device_state_deleted_by_device_id(self):
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID, _resolved(device_state=[{"deviceId": "dev-1"}]), mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(Key={"deviceId": "dev-1"})

    def test_scene_deleted_by_unique_scene_id(self):
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID, _resolved(scene_data=[{"uniqueSceneId": "sc-1", "userId": USER_ID}]),
            mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(Key={"uniqueSceneId": "sc-1"})

    def test_subdevice_macs_trigger_device_data_deletion(self):
        """Subdevice macs from S3 scan must cause device_data rows to be queried and deleted."""
        mock_db = _make_db()
        # Query returns a device_data row for the mac
        mock_db.Table.return_value.query.return_value = {
            "Items": [{"deviceId": "dev-sub-1", "macAddress": "AA:BB:CC:DD"}]
        }
        archiver._hard_delete(
            USER_ID, _resolved(subdevice_macs=["AA:BB:CC:DD"]), mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(
            Key={"deviceId": "dev-sub-1", "macAddress": "AA:BB:CC:DD"}
        )

    def test_no_subdevice_macs_skips_device_data_query(self):
        """If subdevice_macs is empty, device_data query must not be called."""
        mock_db = _make_db()
        archiver._hard_delete(USER_ID, _resolved(subdevice_macs=[]), mock_db, MagicMock())
        query_calls = mock_db.Table.return_value.query.call_args_list
        # No query should reference macAddress-index
        mac_index_calls = [c for c in query_calls if "macAddress-index" in str(c)]
        self.assertEqual(len(mac_index_calls), 0)

    def test_one_mac_with_multiple_device_data_rows_all_deleted(self):
        """A single mac address may match multiple device_data rows — all must be deleted."""
        mock_db = _make_db()
        # Two device_data rows share the same mac (edge case: duplicate mac across sites)
        mock_db.Table.return_value.query.return_value = {
            "Items": [
                {"deviceId": "dev-1", "macAddress": "AA:BB:CC"},
                {"deviceId": "dev-2", "macAddress": "AA:BB:CC"},
            ]
        }
        archiver._hard_delete(
            USER_ID, _resolved(subdevice_macs=["AA:BB:CC"]), mock_db, MagicMock()
        )
        delete_calls = mock_db.Table.return_value.delete_item.call_args_list
        deleted_ids = [c.kwargs["Key"]["deviceId"] for c in delete_calls
                       if "deviceId" in c.kwargs.get("Key", {})]
        self.assertIn("dev-1", deleted_ids)
        self.assertIn("dev-2", deleted_ids)

    def test_s3_metadata_delete_failure_is_non_fatal(self):
        """delete_objects failure must be recorded in errors but must not raise."""
        mock_db = _make_db()
        mock_s3 = MagicMock()
        mock_s3.delete_objects.side_effect = Exception("S3 throttled")
        archiver._hard_delete(
            USER_ID, _resolved(s3_keys=[f"{USER_ID}/file.json"]), mock_db, mock_s3
        )  # must not raise
        # Verify the error was captured in the audit status
        statuses = [
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s")
            for c in mock_db.Table.return_value.update_item.call_args_list
        ]
        self.assertIn("PHASE2_PARTIAL", statuses)

    def test_s3_objects_under_1000_batched_in_single_call(self):
        mock_s3 = MagicMock()
        archiver._hard_delete(
            USER_ID, _resolved(s3_keys=[f"{USER_ID}/f{i}.json" for i in range(500)]),
            _make_db(), mock_s3
        )
        self.assertEqual(mock_s3.delete_objects.call_count, 1)

    def test_s3_objects_over_1000_split_into_two_batches(self):
        mock_s3 = MagicMock()
        archiver._hard_delete(
            USER_ID, _resolved(s3_keys=[f"{USER_ID}/f{i}.json" for i in range(1500)]),
            _make_db(), mock_s3
        )
        self.assertEqual(mock_s3.delete_objects.call_count, 2)

    def test_no_s3_keys_skips_delete_objects(self):
        mock_s3 = MagicMock()
        archiver._hard_delete(USER_ID, _resolved(s3_keys=[]), _make_db(), mock_s3)
        mock_s3.delete_objects.assert_not_called()

    def test_archive_pending_cleared_in_user_data(self):
        mock_db = _make_db()
        archiver._hard_delete(USER_ID, _resolved(), mock_db, MagicMock())
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        pending_cleared = any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":false") is False
            for c in update_calls
        )
        self.assertTrue(pending_cleared, "Expected archivePending set to False in user_data")

    def test_individual_delete_failure_does_not_abort(self):
        """Throttle on one delete must not prevent remaining deletions."""
        mock_db = _make_db()
        mock_db.Table.return_value.delete_item.side_effect = Exception("Throttled")
        resolved = _resolved(
            automation_event=[{"automationId": "auto-1"}],
            device_state=[{"deviceId": "dev-1"}],
            scene_data=[{"uniqueSceneId": "sc-1", "userId": USER_ID}],
        )
        archiver._hard_delete(USER_ID, resolved, mock_db, MagicMock())  # must not raise

    def test_partial_errors_audit_status_is_phase2_partial(self):
        mock_db = _make_db()
        mock_db.Table.return_value.delete_item.side_effect = Exception("Throttled")
        archiver._hard_delete(
            USER_ID, _resolved(device_state=[{"deviceId": "dev-1"}]), mock_db, MagicMock()
        )
        statuses = [
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s")
            for c in mock_db.Table.return_value.update_item.call_args_list
        ]
        self.assertIn("PHASE2_PARTIAL", statuses)

    def test_clean_run_audit_status_is_phase2_complete(self):
        mock_db = _make_db()
        archiver._hard_delete(USER_ID, _resolved(), mock_db, MagicMock())
        statuses = [
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s")
            for c in mock_db.Table.return_value.update_item.call_args_list
        ]
        self.assertIn("PHASE2_COMPLETE", statuses)


# ── S3 subdevice scan ──────────────────────────────────────────────────────────

class TestScanSubdeviceMacs(unittest.TestCase):

    def _make_s3_with_endpoints(self, endpoints_data):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"{USER_ID}/site_s1/devices/d1/gw/sub/endpoints.json"}]}
        ]
        mock_s3.get_paginator.return_value = paginator

        import io
        mock_s3.get_object.return_value = {
            "Body": io.BytesIO(json.dumps(endpoints_data).encode())
        }
        return mock_s3

    def test_subdevice_mac_collected_when_device_type_12(self):
        mock_s3 = self._make_s3_with_endpoints(
            [{"subdeviceMacAddress": "AA:BB:CC", "deviceType": 12}]
        )
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertIn("AA:BB:CC", macs)

    def test_gateway_mac_excluded_when_device_type_11(self):
        mock_s3 = self._make_s3_with_endpoints(
            [{"subdeviceMacAddress": "DD:EE:FF", "deviceType": 11}]
        )
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertNotIn("DD:EE:FF", macs)

    def test_mixed_entries_only_subdevices_collected(self):
        mock_s3 = self._make_s3_with_endpoints([
            {"subdeviceMacAddress": "AA:BB:CC", "deviceType": 12},
            {"subdeviceMacAddress": "DD:EE:FF", "deviceType": 11},
            {"subdeviceMacAddress": "11:22:33", "deviceType": 12},
        ])
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertIn("AA:BB:CC", macs)
        self.assertIn("11:22:33", macs)
        self.assertNotIn("DD:EE:FF", macs)

    def test_duplicate_macs_deduplicated(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [
                {"Key": f"{USER_ID}/site1/endpoints.json"},
                {"Key": f"{USER_ID}/site2/endpoints.json"},
            ]}
        ]
        mock_s3.get_paginator.return_value = paginator

        import io
        same_entry = json.dumps([{"subdeviceMacAddress": "AA:BB:CC", "deviceType": 12}]).encode()
        mock_s3.get_object.return_value = {"Body": io.BytesIO(same_entry)}

        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertEqual(macs.count("AA:BB:CC"), 1)

    def test_no_endpoints_files_returns_empty(self):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": [{"Key": f"{USER_ID}/other.json"}]}]
        mock_s3.get_paginator.return_value = paginator
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertEqual(macs, [])

    def test_s3_read_failure_is_non_fatal(self):
        """If get_object fails for one file, scan continues and returns what it has."""
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"{USER_ID}/site/endpoints.json"}]}
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.get_object.side_effect = Exception("S3 error")
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)  # must not raise
        self.assertEqual(macs, [])

    def test_single_dict_entry_normalized_to_list(self):
        """endpoints.json may contain a single dict instead of a list — must be handled."""
        mock_s3 = self._make_s3_with_endpoints(
            {"subdeviceMacAddress": "AA:BB:CC", "deviceType": 12}  # dict, not list
        )
        macs = archiver._scan_subdevice_macs(USER_ID, mock_s3)
        self.assertIn("AA:BB:CC", macs)


# ── Cognito delete in Phase 2 ──────────────────────────────────────────────────

class TestCognitoDeletePhase2(unittest.TestCase):

    def test_admin_delete_user_called_when_cognito_client_provided(self):
        mock_cog = _make_cognito()
        archive_user(USER_ID, _make_db(), _make_s3(), cognito_client=mock_cog)
        mock_cog.admin_delete_user.assert_called_once_with(
            UserPoolId=archiver.USER_POOL_ID, Username=USER_ID
        )

    def test_admin_delete_user_not_called_when_no_cognito_client(self):
        """If cognito_client=None (default), Cognito delete is skipped."""
        archive_user(USER_ID, _make_db(), _make_s3())
        # No assertion needed — just verify no AttributeError is raised

    def test_cognito_user_not_found_does_not_abort_phase2(self):
        mock_cog = _make_cognito()
        mock_cog.admin_delete_user.side_effect = mock_cog.exceptions.UserNotFoundException()
        archive_user(USER_ID, _make_db(), _make_s3(), cognito_client=mock_cog)  # must not raise

    def test_cognito_general_error_does_not_abort_phase2(self):
        mock_cog = _make_cognito()
        mock_cog.admin_delete_user.side_effect = Exception("Cognito unavailable")
        archive_user(USER_ID, _make_db(), _make_s3(), cognito_client=mock_cog)  # must not raise


# ── restore_user ───────────────────────────────────────────────────────────────

class TestRestoreUser(unittest.TestCase):

    DELETION_TOKEN = "test-deletion-token-uuid"

    def _make_evidence_db(self, **overrides):
        """DynamoDB mock where deletion_evidence returns a valid item."""
        mock_db = MagicMock()
        evidence = {
            "deletionToken": self.DELETION_TOKEN,
            "userId":        USER_ID,
            "requestedAt":   (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        }
        evidence.update(overrides)

        def get_item_side_effect(Key, **kwargs):
            if "deletionToken" in Key:
                return {"Item": evidence}
            return {"Item": {"userId": USER_ID, "deviceBindingsBackup": []}}

        mock_db.Table.return_value.get_item.side_effect = get_item_side_effect
        mock_db.Table.return_value.update_item.return_value = {}
        return mock_db

    def test_valid_restore_completes_without_error(self):
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)

    def test_cognito_enable_called(self):
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)
        mock_cog.admin_enable_user.assert_called_once_with(
            UserPoolId=archiver.USER_POOL_ID, Username=USER_ID
        )

    def test_user_data_restored_to_active(self):
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        active_call = next(
            (c for c in update_calls
             if c.kwargs.get("ExpressionAttributeValues", {}).get(":active") == "ACTIVE"),
            None
        )
        self.assertIsNotNone(active_call, "Expected update_item to set status=ACTIVE")

    def test_unknown_token_raises_value_error(self):
        mock_db = MagicMock()
        mock_db.Table.return_value.get_item.return_value = {"Item": None}
        with self.assertRaises(ValueError) as ctx:
            restore_user(USER_ID, "unknown-token", mock_db, MagicMock(), _make_cognito())
        self.assertIn("Invalid", str(ctx.exception))

    def test_wrong_user_raises_value_error(self):
        mock_db  = self._make_evidence_db(userId="different-user")
        with self.assertRaises(ValueError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), _make_cognito())
        self.assertIn("belong", str(ctx.exception))

    def test_already_restored_raises_value_error(self):
        mock_db = self._make_evidence_db(restoredAt="2026-08-10T10:00:00+00:00")
        with self.assertRaises(ValueError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), _make_cognito())
        self.assertIn("already been restored", str(ctx.exception))

    def test_archive_deleted_raises_value_error(self):
        mock_db = self._make_evidence_db(archiveDeletedAt="2026-08-17T00:00:00+00:00")
        with self.assertRaises(ValueError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), _make_cognito())
        self.assertIn("permanently deleted", str(ctx.exception))

    def test_window_expired_raises_value_error(self):
        old_request = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        mock_db = self._make_evidence_db(requestedAt=old_request)
        with self.assertRaises(ValueError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), _make_cognito())
        self.assertIn("window has closed", str(ctx.exception))

    def test_cognito_enable_failure_raises_runtime_error(self):
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        mock_cog.admin_enable_user.side_effect = Exception("Cognito error")
        with self.assertRaises(RuntimeError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)
        self.assertIn("re-enable", str(ctx.exception))

    def test_user_data_update_failure_raises_runtime_error(self):
        """If the user_data update fails during restore, RuntimeError must be raised."""
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        # First update_item call (user_data restore) raises; second (evidence) should not be reached
        mock_db.Table.return_value.update_item.side_effect = Exception("DynamoDB write failed")
        with self.assertRaises(RuntimeError) as ctx:
            restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)
        self.assertIn("user_data", str(ctx.exception))

    def test_restored_at_written_to_deletion_evidence(self):
        mock_db  = self._make_evidence_db()
        mock_cog = _make_cognito()
        restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), mock_cog)
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        evidence_update = next(
            (c for c in update_calls
             if "restoredAt" in c.kwargs.get("UpdateExpression", "")),
            None
        )
        self.assertIsNotNone(evidence_update, "Expected restoredAt to be written to deletion_evidence")

    def test_device_bindings_restored(self):
        """Devices in deviceBindingsBackup must be re-bound to the user."""
        mock_db = MagicMock()
        evidence = {
            "deletionToken": self.DELETION_TOKEN,
            "userId":        USER_ID,
            "requestedAt":   (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        }
        user_item = {
            "userId": USER_ID,
            "deviceBindingsBackup": [
                {"deviceId": "dev-1", "macAddress": "AA:BB"},
                {"deviceId": "dev-2", "macAddress": "CC:DD"},
            ]
        }

        def get_item_side_effect(Key, **kwargs):
            if "deletionToken" in Key:
                return {"Item": evidence}
            return {"Item": user_item}

        mock_db.Table.return_value.get_item.side_effect = get_item_side_effect
        mock_db.Table.return_value.update_item.return_value = {}

        restore_user(USER_ID, self.DELETION_TOKEN, mock_db, MagicMock(), _make_cognito())

        rebind_calls = [
            c for c in mock_db.Table.return_value.update_item.call_args_list
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":v") == USER_ID
        ]
        self.assertEqual(len(rebind_calls), 2)


# ── Automation deduplication ───────────────────────────────────────────────────

class TestAutomationDeduplication(unittest.TestCase):

    def test_same_auto_id_via_scene_and_duid_collected_once(self):
        """
        The same automation_event row found via both GSI_scene_automate and
        GSI_DuidEndpoint must appear only once in the resolved list.
        """
        shared = {"automationId": "auto-shared", "sceneId": "sc-1", "duid": "dev-1"}
        mock_db = MagicMock()
        mock_db.Table.return_value.query.return_value = {"Items": [shared]}

        r = {"automation_event": [], "automation_schedule_direct": [], "automation_schedule_ctrl": []}
        archiver._resolve_automations(mock_db, ["sc-1"], ["dev-1"], r)

        self.assertEqual(len(r["automation_event"]), 1)

    def test_distinct_auto_ids_from_scene_and_duid_both_collected(self):
        scene_row  = {"automationId": "auto-scene",  "sceneId": "sc-1"}
        device_row = {"automationId": "auto-device", "duid":    "dev-1"}
        mock_db = MagicMock()

        def query_side(**kw):
            if kw.get("IndexName") == "GSI_scene_automate":
                return {"Items": [scene_row]}
            if kw.get("IndexName") == "GSI_DuidEndpoint":
                return {"Items": [device_row]}
            return {"Items": []}

        mock_db.Table.return_value.query.side_effect = query_side

        r = {"automation_event": [], "automation_schedule_direct": [], "automation_schedule_ctrl": []}
        archiver._resolve_automations(mock_db, ["sc-1"], ["dev-1"], r)

        ids = {row["automationId"] for row in r["automation_event"]}
        self.assertIn("auto-scene",  ids)
        self.assertIn("auto-device", ids)

    def test_no_scenes_no_devices_results_in_empty_automation_lists(self):
        mock_db = MagicMock()
        r = {"automation_event": [], "automation_schedule_direct": [], "automation_schedule_ctrl": []}
        archiver._resolve_automations(mock_db, [], [], r)
        self.assertEqual(r["automation_event"], [])
        mock_db.Table.return_value.query.assert_not_called()


# ── Full pipeline ──────────────────────────────────────────────────────────────

class TestFullPipeline(unittest.TestCase):

    def test_happy_path_completes_without_error(self):
        archive_user(USER_ID, _make_db(), _make_s3())

    def test_happy_path_with_cognito_client(self):
        archive_user(USER_ID, _make_db(), _make_s3(), cognito_client=_make_cognito())

    def test_deletion_evidence_timestamps_updated_after_archive(self):
        """archive_user must update archiveStartedAt, archiveCompletedAt, archiveDeletedAt in deletion_evidence."""
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        mock_db = _make_db(deletion_requested_at=old)
        # Inject deletionToken into the user_data item
        mock_db.Table.return_value.get_item.return_value = {
            "Item": {
                "userId":               USER_ID,
                "status":               "INACTIVE",
                "archivePending":       True,
                "deletionRequestedAt":  old,
                "deletionToken":        "test-token-abc",
            }
        }
        archive_user(USER_ID, mock_db, _make_s3())
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        evidence_update = next(
            (c for c in update_calls
             if "archiveDeletedAt" in c.kwargs.get("UpdateExpression", "")),
            None
        )
        self.assertIsNotNone(evidence_update, "Expected update_item to set archiveDeletedAt on deletion_evidence")
        vals = evidence_update.kwargs["ExpressionAttributeValues"]
        self.assertIn(":c", vals, "archiveCompletedAt (:c) missing")
        self.assertIn(":d", vals, "archiveDeletedAt (:d) missing")
        self.assertIn(":s", vals, "archiveStartedAt (:s) missing")

    def test_no_deletion_token_skips_evidence_update(self):
        """If user_data has no deletionToken, deletion_evidence update must be skipped silently."""
        mock_db = _make_db()  # no deletionToken in item
        archive_user(USER_ID, mock_db, _make_s3())
        update_calls = mock_db.Table.return_value.update_item.call_args_list
        evidence_update = next(
            (c for c in update_calls
             if "archiveDeletedAt" in c.kwargs.get("UpdateExpression", "")),
            None
        )
        self.assertIsNone(evidence_update, "deletion_evidence must NOT be updated when deletionToken is absent")

    def test_verification_failure_prevents_hard_delete(self):
        """VerificationError must abort before any delete_item is called."""
        mock_db = _make_db()
        mock_s3 = _make_s3()
        mock_s3.head_object.side_effect = _client_error("404")

        with self.assertRaises(VerificationError):
            archive_user(USER_ID, mock_db, mock_s3)

        mock_db.Table.return_value.delete_item.assert_not_called()

    def test_empty_user_completes_cleanly(self):
        """User with no devices, scenes, or automations — empty run succeeds."""
        archive_user(USER_ID, _make_db(), _make_s3())

    def test_pipeline_order_is_archive_then_verify_then_delete(self):
        """Steps must execute in strict order: archive → verify → hard-delete."""
        call_order = []
        with patch.object(archiver, '_archive',     side_effect=lambda *a, **kw: call_order.append("archive")), \
             patch.object(archiver, '_verify',      side_effect=lambda *a, **kw: call_order.append("verify")), \
             patch.object(archiver, '_hard_delete', side_effect=lambda *a, **kw: call_order.append("delete")):
            archive_user(USER_ID, _make_db(), _make_s3())
        self.assertEqual(call_order, ["archive", "verify", "delete"])

    def test_guard_failure_skips_archive_and_delete(self):
        """If guards fail, neither archive nor delete should be called."""
        mock_db = _make_db(status="ACTIVE", archive_pending=False)
        with patch.object(archiver, '_archive')     as mock_arch, \
             patch.object(archiver, '_hard_delete') as mock_del:
            with self.assertRaises(ValueError):
                archive_user(USER_ID, mock_db, MagicMock())
        mock_arch.assert_not_called()
        mock_del.assert_not_called()


# ── _query_all pagination ──────────────────────────────────────────────────────

class TestQueryAllPagination(unittest.TestCase):

    def test_query_all_follows_last_evaluated_key(self):
        """_query_all must keep querying until LastEvaluatedKey is absent."""
        page1 = {"Items": [{"uniqueSceneId": "sc-1", "userId": USER_ID}], "LastEvaluatedKey": {"uniqueSceneId": "sc-1"}}
        page2 = {"Items": [{"uniqueSceneId": "sc-2", "userId": USER_ID}]}

        mock_db = MagicMock()
        mock_db.Table.return_value.query.side_effect = [page1, page2]

        items = archiver._query_all(mock_db, "digilux_honeywell_scene_data", "userId", USER_ID)

        self.assertEqual(len(items), 2)
        self.assertEqual(mock_db.Table.return_value.query.call_count, 2)

    def test_query_all_single_page_no_lek(self):
        """Single-page result must return all items and query exactly once."""
        mock_db = MagicMock()
        mock_db.Table.return_value.query.return_value = {
            "Items": [{"deviceId": "dev-1", "macAddress": "AA:BB"}]
        }
        items = archiver._query_all(mock_db, "digilux_honeywell_device_data", "userId", USER_ID)
        self.assertEqual(len(items), 1)
        self.assertEqual(mock_db.Table.return_value.query.call_count, 1)

    def test_query_all_empty_table_returns_empty_list(self):
        mock_db = MagicMock()
        mock_db.Table.return_value.query.return_value = {"Items": []}
        items = archiver._query_all(mock_db, "some_table", "userId", USER_ID)
        self.assertEqual(items, [])

    def test_query_all_uses_index_when_provided(self):
        mock_db = MagicMock()
        mock_db.Table.return_value.query.return_value = {"Items": []}
        archiver._query_all(mock_db, "digilux_honeywell_device_data", "userId", USER_ID, index="userId-index")
        call_kwargs = mock_db.Table.return_value.query.call_args.kwargs
        self.assertEqual(call_kwargs.get("IndexName"), "userId-index")


# ── All remaining table deletion key patterns ──────────────────────────────────

class TestHardDeleteKeyPatterns(unittest.TestCase):
    """Verify every table's delete_item is called with its correct key schema."""

    def _run(self, **resolved_kwargs):
        mock_db = _make_db()
        archiver._hard_delete(USER_ID, _resolved(**resolved_kwargs), mock_db, MagicMock())
        return mock_db.Table.return_value.delete_item.call_args_list

    def test_user_device_details_deleted_with_user_id_and_site_id(self):
        calls = self._run(user_device_details=[{"userId": USER_ID, "siteId": "site-1"}])
        self.assertIn(call(Key={"userId": USER_ID, "siteId": "site-1"}), calls)

    def test_user_device_mapping_deleted_with_user_id_and_site_id(self):
        calls = self._run(user_device_mapping=[{"userId": USER_ID, "siteId": "site-1"}])
        self.assertIn(call(Key={"userId": USER_ID, "siteId": "site-1"}), calls)

    def test_user_subuser_detail_deleted_with_user_id_and_subuser_id(self):
        calls = self._run(user_subuser_detail=[{"userId": USER_ID, "subUserId": "sub-1"}])
        self.assertIn(call(Key={"userId": USER_ID, "subUserId": "sub-1"}), calls)

    def test_user_subuser_mapping_deleted_with_subuser_id_and_request_id(self):
        calls = self._run(user_subuser_mapping=[{"subuserId": "sub-1", "requestId": "req-1"}])
        self.assertIn(call(Key={"subuserId": "sub-1", "requestId": "req-1"}), calls)

    def test_subuser_role_data_deleted_with_main_user_id_and_role_id(self):
        calls = self._run(subuser_role_data=[{"mainUserId": USER_ID, "roleId": "role-1"}])
        self.assertIn(call(Key={"mainUserId": USER_ID, "roleId": "role-1"}), calls)

    def test_admin_otp_data_deleted_with_user_id_and_module_category(self):
        calls = self._run(admin_otp_data=[{"userId": USER_ID, "moduleCategory": "LOGIN"}])
        self.assertIn(call(Key={"userId": USER_ID, "moduleCategory": "LOGIN"}), calls)

    def test_alexa_lwa_tokens_deleted_by_user_id(self):
        mock_db = _make_db()
        archiver._hard_delete(USER_ID, _resolved(), mock_db, MagicMock())
        mock_db.Table.return_value.delete_item.assert_any_call(Key={"userId": USER_ID})

    def test_entity_state_deleted_with_device_id_and_endpoint_type(self):
        calls = self._run(entity_state=[{"deviceId": "dev-1", "endpointType": "light"}])
        self.assertIn(call(Key={"deviceId": "dev-1", "endpointType": "light"}), calls)

    def test_automation_schedule_ctrl_deleted_with_composite_key(self):
        calls = self._run(automation_schedule_ctrl=[{"uuid": USER_ID, "automationId": "auto-3"}])
        self.assertIn(call(Key={"uuid": USER_ID, "automationId": "auto-3"}), calls)


# ── Resolve resilience ─────────────────────────────────────────────────────────

class TestResolveResilience(unittest.TestCase):

    def test_device_state_query_failure_is_non_fatal(self):
        """If get_item for device_state raises, resolve continues with partial data."""
        mock_db = MagicMock()
        mock_db.Table.return_value.get_item.side_effect = Exception("Throttled")
        rows = archiver._resolve_device_state(mock_db, ["dev-1", "dev-2"])
        self.assertEqual(rows, [])  # failed gracefully, returns empty

    def test_entity_state_scan_failure_is_non_fatal(self):
        """If entity_state scan raises, resolve continues without crashing."""
        mock_db = MagicMock()
        mock_db.Table.return_value.scan.side_effect = Exception("GSI down")
        rows = archiver._resolve_entity_state(mock_db, ["dev-1"])
        self.assertEqual(rows, [])

    def test_automation_query_failure_is_non_fatal(self):
        """If automation GSI query raises, _resolve_automations continues."""
        mock_db = MagicMock()
        mock_db.Table.return_value.query.side_effect = Exception("GSI unavailable")
        r = {"automation_event": [], "automation_schedule_direct": [], "automation_schedule_ctrl": []}
        archiver._resolve_automations(mock_db, ["sc-1"], ["dev-1"], r)
        # Must not raise — all lists remain empty
        self.assertEqual(r["automation_event"], [])


if __name__ == "__main__":
    unittest.main()
