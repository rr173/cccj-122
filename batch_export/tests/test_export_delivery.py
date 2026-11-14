"""End-to-end tests for the auditable batch export delivery service."""
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from export_delivery import (  # noqa: E402
    ExportRequest,
    ExportService,
    InvalidStateError,
    JobStatus,
    LocalWarehouse,
    Receipt,
    ReceiptOutcome,
    ReceiverSimulator,
    ShardStatus,
    SourceDataChangedError,
    UnknownFieldError,
    UnknownJobError,
    UnknownVersionError,
)

ROW_COUNT = 250
SHARD_SIZE = 10  # 250 rows / 10 -> 25 shards for the default request


def make_request(**overrides) -> ExportRequest:
    base = dict(
        query_version="v1",
        projection=("id", "country", "amount"),
        filters={},
        receiver="receiver-a",
        shard_size=SHARD_SIZE,
    )
    base.update(overrides)
    return ExportRequest(**base)


class ExportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "state.db")
        self.warehouse = LocalWarehouse(row_count=ROW_COUNT)
        self.warehouse.register_version("v1")
        self.service = ExportService(self.db_path, self.warehouse)
        self.addCleanup(lambda: self.service.close())
        self.receiver = ReceiverSimulator("receiver-a")

    # -- drivers --------------------------------------------------------------

    def drive_once(self, job_id, limit=None):
        """Dispatch unacked shards, deliver them, record every receipt."""
        outcomes = []
        for payload in self.service.dispatch_unacknowledged(job_id, limit=limit):
            for receipt in self.receiver.deliver(payload):
                outcomes.append(self.service.record_receipt(receipt))
        return outcomes

    def drive_to_completion(self, job_id, max_rounds=50):
        for _ in range(max_rounds):
            job = self.service.get_job(job_id)
            if job.status in (JobStatus.DELIVERED, JobStatus.REVOKED, JobStatus.CONFLICT):
                return job
            self.drive_once(job_id)
        return self.service.get_job(job_id)


class TestRequestIdentity(ExportTestBase):
    def test_identity_is_stable_and_field_sensitive(self):
        a = make_request(filters={"country": "CN", "channel": "web"})
        b = make_request(filters={"channel": "web", "country": "CN"})
        self.assertEqual(a.request_id, b.request_id, "filter key order must not matter")
        self.assertNotEqual(a.request_id, make_request(filters={"country": "US"}).request_id)
        self.assertNotEqual(a.request_id, make_request(query_version="v2").request_id)
        self.assertNotEqual(
            a.request_id,
            make_request(projection=("amount", "country", "id")).request_id,
            "projection order is part of the identity",
        )
        self.assertNotEqual(a.request_id, make_request(receiver="receiver-b").request_id)
        self.assertNotEqual(a.request_id, make_request(shard_size=25).request_id)

    def test_resubmit_is_idempotent(self):
        job1 = self.service.submit_export(make_request())
        job2 = self.service.submit_export(make_request())
        self.assertEqual(job1.job_id, job2.job_id)
        self.assertEqual(job1.job_id, make_request().request_id)
        self.assertEqual(len(self.service.list_jobs()), 1)
        self.assertEqual(len(self.service.manifest(job1.job_id)), job1.shard_count)

    def test_invalid_requests_are_rejected(self):
        with self.assertRaises(UnknownVersionError):
            self.service.submit_export(make_request(query_version="v9"))
        with self.assertRaises(UnknownFieldError):
            self.service.submit_export(make_request(projection=("id", "nope")))


class TestEmptyResult(ExportTestBase):
    def test_empty_result_completes_immediately(self):
        job = self.service.submit_export(make_request(filters={"country": "ZZ"}))
        self.assertEqual(job.status, JobStatus.DELIVERED)
        self.assertEqual(job.shard_count, 0)
        self.assertEqual(job.row_count, 0)
        self.assertIsNotNone(job.completed_at)
        trail = self.service.audit_trail(job.job_id)
        self.assertEqual(trail["request"]["filters"], {"country": "ZZ"})
        self.assertEqual(trail["manifest"], [])
        self.assertEqual(trail["receipts"], [])
        with self.assertRaises(InvalidStateError):
            self.service.dispatch_unacknowledged(job.job_id)


class TestOrderingStability(ExportTestBase):
    def test_manifest_stable_and_rows_globally_ordered(self):
        job = self.service.submit_export(make_request())
        self.assertEqual(job.shard_count, ROW_COUNT // SHARD_SIZE)
        digests_before = [s.digest for s in self.service.manifest(job.job_id)]

        # Reopen the state store (process restart): manifest must be unchanged.
        self.service.close()
        self.service = ExportService(self.db_path, self.warehouse)
        self.assertEqual(digests_before, [s.digest for s in self.service.manifest(job.job_id)])

        payloads = self.service.dispatch_unacknowledged(job.job_id)
        self.assertEqual(len(payloads), job.shard_count)
        seen_ids = []
        for payload in payloads:
            shard_ids = [row[0] for row in payload.rows]
            self.assertEqual(shard_ids, sorted(shard_ids), "rows sorted within a shard")
            seen_ids.extend(shard_ids)
        self.assertEqual(seen_ids, list(range(1, ROW_COUNT + 1)))
        self.assertEqual(seen_ids, sorted(seen_ids), "ordering is stable across shards")

        manifest = self.service.manifest(job.job_id)
        for shard, payload in zip(manifest, payloads):
            self.assertEqual(shard.digest, payload.digest)


class TestFilters(ExportTestBase):
    def test_projection_and_range_filters_applied(self):
        job = self.service.submit_export(
            make_request(
                filters={"country": "CN", "amount": {"lt": 500000}},
                projection=("id", "amount"),
            )
        )
        self.assertGreater(job.row_count, 0)
        payloads = self.service.dispatch_unacknowledged(job.job_id)
        delivered = sum(len(p.rows) for p in payloads)
        self.assertEqual(delivered, job.row_count)
        for payload in payloads:
            self.assertEqual(payload.projection, ("id", "amount"))
            for row in payload.rows:
                self.assertLess(row[1], 500000)


class TestDuplicateReceipts(ExportTestBase):
    def test_exact_replay_and_fresh_duplicate(self):
        job = self.service.submit_export(make_request())
        payloads = self.service.dispatch_unacknowledged(job.job_id)
        receipt = self.receiver.deliver(payloads[0])[0]
        self.assertEqual(self.service.record_receipt(receipt), ReceiptOutcome.ACCEPTED)
        self.assertEqual(
            self.service.record_receipt(receipt),
            ReceiptOutcome.DUPLICATE,
            "exact replay must be a no-op",
        )
        dupe = Receipt.issue(job.job_id, receipt.shard_ordinal, receipt.digest, "receiver-a")
        self.assertEqual(
            self.service.record_receipt(dupe),
            ReceiptOutcome.DUPLICATE,
            "a new receipt for an acknowledged shard is a duplicate",
        )
        for payload in payloads[1:]:
            for r in self.receiver.deliver(payload):
                self.service.record_receipt(r)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)
        receipts = self.service.audit_trail(job.job_id)["receipts"]
        # replayed receipt stored once + the fresh-id duplicate is kept for audit
        self.assertEqual(len(receipts), job.shard_count + 1)
        outcomes = [r["outcome"] for r in receipts]
        self.assertEqual(outcomes.count("ACCEPTED"), job.shard_count)
        self.assertEqual(outcomes.count("DUPLICATE"), 1)

    def test_receiver_level_duplicates(self):
        self.receiver.send_duplicates = True
        job = self.service.submit_export(make_request())
        outcomes = self.drive_once(job.job_id)
        self.assertEqual(outcomes.count(ReceiptOutcome.ACCEPTED), job.shard_count)
        self.assertEqual(outcomes.count(ReceiptOutcome.DUPLICATE), job.shard_count)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)


class TestOutOfOrderReceipts(ExportTestBase):
    def test_shuffled_receipts_all_accepted(self):
        job = self.service.submit_export(make_request())
        payloads = self.service.dispatch_unacknowledged(job.job_id)
        receipts = [r for p in payloads for r in self.receiver.deliver(p)]
        random.Random(42).shuffle(receipts)
        outcomes = [self.service.record_receipt(r) for r in receipts]
        self.assertEqual(set(outcomes), {ReceiptOutcome.ACCEPTED})
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)


class TestDigestConflict(ExportTestBase):
    def test_wrong_digest_enters_conflict_state(self):
        self.receiver.corrupt_ordinals = {3}
        job = self.service.submit_export(make_request())
        outcomes = self.drive_once(job.job_id)
        self.assertIn(ReceiptOutcome.CONFLICT, outcomes)

        job = self.service.get_job(job.job_id)
        self.assertEqual(job.status, JobStatus.CONFLICT)
        shards = {s.ordinal: s for s in self.service.manifest(job.job_id)}
        self.assertEqual(shards[3].status, ShardStatus.CONFLICT)
        self.assertEqual(shards[2].status, ShardStatus.ACKED)

        conflict = [r for r in self.service.audit_trail(job.job_id)["receipts"]
                    if r["outcome"] == "CONFLICT"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0]["shard_ordinal"], 3)
        self.assertNotEqual(conflict[0]["digest"], shards[3].digest,
                            "conflicting receipt is preserved for audit")

        again = Receipt.issue(job.job_id, 3, shards[3].digest, "receiver-a")
        self.assertEqual(self.service.record_receipt(again), ReceiptOutcome.REJECTED)
        with self.assertRaises(InvalidStateError):
            self.service.dispatch_unacknowledged(job.job_id)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.CONFLICT)

        # a conflicted delivery can still be revoked
        self.assertEqual(self.service.revoke(job.job_id).status, JobStatus.REVOKED)


class TestVersionChange(ExportTestBase):
    def test_version_change_does_not_mix_into_started_export(self):
        request = make_request()
        job = self.service.submit_export(request)
        first = self.service.dispatch_unacknowledged(job.job_id, limit=5)
        for payload in first:
            for r in self.receiver.deliver(payload):
                self.service.record_receipt(r)

        # upstream registers and switches to a new version mid-export
        self.warehouse.register_version("v2")
        self.warehouse.set_current_version("v2")
        v1_rows = self.warehouse.query(version="v1", projection=request.projection, filters={})
        v2_rows = self.warehouse.query(version="v2", projection=request.projection, filters={})
        self.assertNotEqual(v1_rows, v2_rows, "versions must actually differ")

        # remaining shards are still cut from the pinned v1 snapshot
        rest = self.service.dispatch_unacknowledged(job.job_id)
        self.assertEqual(len(rest), job.shard_count - 5)
        for payload in rest:
            start = payload.shard_ordinal * SHARD_SIZE
            self.assertEqual([list(r) for r in payload.rows], v1_rows[start:start + SHARD_SIZE])
        for payload in rest:
            for r in self.receiver.deliver(payload):
                self.assertEqual(self.service.record_receipt(r), ReceiptOutcome.ACCEPTED)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)

        # the same logical request at v2 is a different export, not a mix-in
        job_v2 = self.service.submit_export(make_request(query_version="v2"))
        self.assertNotEqual(job.job_id, job_v2.job_id)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)

    def test_silent_source_change_is_detected(self):
        job = self.service.submit_export(make_request())
        self.service.dispatch_unacknowledged(job.job_id, limit=2)
        # someone redefines the pinned version's data underneath the export
        self.warehouse.register_version("v1", seed="tampered-seed")
        with self.assertRaises(SourceDataChangedError):
            self.service.dispatch_unacknowledged(job.job_id)


class TestPartialRetry(ExportTestBase):
    def test_only_unacknowledged_shards_are_resent(self):
        self.receiver.drop_ordinals = {2, 5}
        job = self.service.submit_export(make_request())
        outcomes = self.drive_once(job.job_id)
        self.assertEqual(len(outcomes), job.shard_count - 2)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERING)

        self.receiver.inbox.clear()
        self.receiver.drop_ordinals.clear()
        retry = self.service.dispatch_unacknowledged(job.job_id)
        self.assertEqual(sorted(p.shard_ordinal for p in retry), [2, 5],
                         "only unconfirmed shards are resent")
        for payload in retry:
            for r in self.receiver.deliver(payload):
                self.service.record_receipt(r)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)

        manifest = {s.ordinal: s for s in self.service.manifest(job.job_id)}
        self.assertEqual(manifest[2].send_count, 2)
        self.assertEqual(manifest[5].send_count, 2)
        self.assertEqual(manifest[0].send_count, 1)
        sends = self.service.audit_trail(job.job_id)["sends"]
        self.assertEqual(len(sends), job.shard_count + 2, "every attempt is logged")


class TestRevoke(ExportTestBase):
    def test_revoke_incomplete_delivery(self):
        job = self.service.submit_export(make_request())
        payloads = self.service.dispatch_unacknowledged(job.job_id, limit=3)
        self.service.revoke(job.job_id)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.REVOKED)

        with self.assertRaises(InvalidStateError):
            self.service.dispatch_unacknowledged(job.job_id)

        late = self.receiver.deliver(payloads[0])[0]
        self.assertEqual(self.service.record_receipt(late), ReceiptOutcome.REJECTED)
        receipts = self.service.audit_trail(job.job_id)["receipts"]
        self.assertEqual(receipts[0]["outcome"], "REJECTED",
                         "late receipts are rejected but preserved")

        self.service.revoke(job.job_id)  # idempotent
        again = self.service.submit_export(make_request())
        self.assertEqual(again.job_id, job.job_id)
        self.assertEqual(again.status, JobStatus.REVOKED,
                         "resubmission keeps the stable identity and audit trail")

    def test_cannot_revoke_completed_export(self):
        job = self.service.submit_export(make_request())
        self.drive_to_completion(job.job_id)
        self.assertEqual(self.service.get_job(job.job_id).status, JobStatus.DELIVERED)
        with self.assertRaises(InvalidStateError):
            self.service.revoke(job.job_id)


class TestProcessRecovery(ExportTestBase):
    def test_resume_after_process_restart(self):
        job = self.service.submit_export(make_request())
        first = self.service.dispatch_unacknowledged(job.job_id, limit=4)
        for payload in first[:2]:  # acknowledge only 2 of the 4 sent shards
            for r in self.receiver.deliver(payload):
                self.service.record_receipt(r)
        self.service.close()  # simulate process crash / restart

        self.service = ExportService(self.db_path, self.warehouse)
        incomplete = self.service.recover_incomplete()
        self.assertEqual([j.job_id for j in incomplete], [job.job_id])
        manifest = {s.ordinal: s for s in self.service.manifest(job.job_id)}
        self.assertEqual(manifest[0].status, ShardStatus.ACKED)
        self.assertEqual(manifest[2].status, ShardStatus.SENT)
        self.assertEqual(manifest[4].status, ShardStatus.PENDING)

        self.receiver.inbox.clear()
        resumed = self.drive_to_completion(job.job_id)
        self.assertEqual(resumed.status, JobStatus.DELIVERED)
        resent = sorted(p.shard_ordinal for p in self.receiver.inbox)
        self.assertEqual(resent, list(range(2, job.shard_count)),
                         "recovery resends exactly the unacknowledged shards")

        trail = self.service.audit_trail(job.job_id)
        self.assertEqual(len(trail["receipts"]), job.shard_count,
                         "pre-crash receipts survived the restart")
        manifest_ordinals = {s.ordinal for s in trail["manifest"]}
        for receipt in trail["receipts"]:
            self.assertEqual(receipt["job_id"], job.job_id)
            self.assertIn(receipt["shard_ordinal"], manifest_ordinals)


class TestReceiptGuards(ExportTestBase):
    def test_unknown_job_raises(self):
        ghost = Receipt.issue("exp_nonexistent", 0, "x", "receiver-a")
        with self.assertRaises(UnknownJobError):
            self.service.record_receipt(ghost)

    def test_unknown_shard_and_wrong_receiver_rejected(self):
        job = self.service.submit_export(make_request())
        self.service.dispatch_unacknowledged(job.job_id)
        bad_ordinal = Receipt.issue(job.job_id, 999, "x", "receiver-a")
        self.assertEqual(self.service.record_receipt(bad_ordinal), ReceiptOutcome.REJECTED)
        wrong_receiver = Receipt.issue(job.job_id, 0, "x", "receiver-b")
        self.assertEqual(self.service.record_receipt(wrong_receiver), ReceiptOutcome.REJECTED)

    def test_receipt_for_undispatched_shard_rejected(self):
        job = self.service.submit_export(make_request())
        shard = self.service.manifest(job.job_id)[0]
        early = Receipt.issue(job.job_id, shard.ordinal, shard.digest, "receiver-a")
        self.assertEqual(self.service.record_receipt(early), ReceiptOutcome.REJECTED)


if __name__ == "__main__":
    unittest.main()
