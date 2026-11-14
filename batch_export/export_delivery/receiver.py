"""Simulated downstream receiver used by tests and demos.

Default behavior: acknowledge every shard with a correct digest.
Test hooks:
  drop_ordinals    -- never return a receipt for these shards (loss)
  corrupt_ordinals -- return a receipt with a wrong digest
  send_duplicates  -- return every receipt twice (same receipt id)
"""
from __future__ import annotations

import hashlib

from .models import Receipt, ShardPayload


class ReceiverSimulator:
    def __init__(self, name: str):
        self.name = name
        self.inbox: list[ShardPayload] = []
        self.drop_ordinals: set[int] = set()
        self.corrupt_ordinals: set[int] = set()
        self.send_duplicates: bool = False

    def deliver(self, payload: ShardPayload) -> list[Receipt]:
        self.inbox.append(payload)
        if payload.shard_ordinal in self.drop_ordinals:
            return []
        digest = payload.digest
        if payload.shard_ordinal in self.corrupt_ordinals:
            digest = hashlib.sha256(
                f"corrupt:{payload.job_id}:{payload.shard_ordinal}".encode("utf-8")
            ).hexdigest()
        receipt = Receipt.issue(
            job_id=payload.job_id,
            shard_ordinal=payload.shard_ordinal,
            digest=digest,
            receiver=self.name,
        )
        receipts = [receipt]
        if self.send_duplicates:
            receipts.append(receipt)
        return receipts
