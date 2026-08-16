"""
Phase 2 Archiver Tests
Covers: guard checks, archive, verify, hard-delete, deduplication, full pipeline ordering.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch, call
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))

import archiver
from archiver import archive_user, VerificationError

USER_ID         = "cognito-sub-uuid-abc-123"
ARCHIVE_BUCKET  = "digilux-honeywell-archive"
METADATA_BUCKET = "digilux-honeywell-metadata"

ALL_COLLECTIONS = [
    "device_data", "scene_data", "user_device_details", "user_device_mapping",
    "user_subuser_detail", "user_subuser_mapping", "subuser_role_data",
    "admin_otp_data", "alexa_lwa_tokens", "device_state", "entity_state",
    "automation_event", "automation_schedule_direct", "automation_schedule_ctrl",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "op")


def _resolved(**overrides):
    """Build a resolved dict; all collections empty by default."""
    base = {name: [] for name in ALL_COLLECTIONS}
    base["s3_keys"] = []
    base.update(overrides)
    return base


def _make_db(status="INACTIVE", archive_pending=True):
    """DynamoDB mock: get_item returns a valid INACTIVE user; queries return empty Items."""
    mock_db = MagicMock()
    user_item = {"userId": USER_ID, "status": status, "archivePending": archive_pending}
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


# ── Guard checks ───────────────────────────────────────────────────────────────

class TestArchiveUserGuards(unittest.TestCase):

    def _db_for(self, status, archive_pending, exists=True):
        mock_db = MagicMock()
        item = {"userId": USER_ID, "status": status, "archivePending": archive_pending} if exists else None
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

    def test_archived_json_contains_correct_device_data(self):
        mock_s3 = MagicMock()
        devices = [{"deviceId": "dev-1", "macAddress": "AA:BB"}]
        archiver._archive(USER_ID, _resolved(device_data=devices), mock_s3)
        device_call = next(
            c for c in mock_s3.put_object.call_args_list
            if "device_data" in c.kwargs["Key"]
        )
        body = json.loads(device_call.kwargs["Body"])
        self.assertEqual(body[0]["deviceId"], "dev-1")

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

    def test_device_data_deleted_with_composite_key(self):
        mock_db = _make_db()
        archiver._hard_delete(
            USER_ID, _resolved(device_data=[{"deviceId": "dev-1", "macAddress": "AA:BB"}]),
            mock_db, MagicMock()
        )
        mock_db.Table.return_value.delete_item.assert_any_call(
            Key={"deviceId": "dev-1", "macAddress": "AA:BB"}
        )

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
