"""Tests for the zero-external-service adapters.

These three adapters are what a developer runs *by default* on the SQLite path the README
recommends (``REDIS_URL=`` selects the in-memory ones). They are the least glamorous code
in the tree and the easiest to leave untested, which is precisely how a divergence
between local behaviour and production behaviour survives to the day someone points the
app at real Redis and S3.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.platform.cache.memory import InMemoryCache
from app.platform.queue.memory import InMemoryQueue
from app.platform.queue.port import Job, JobName
from app.platform.storage.local import LocalObjectStorage
from app.platform.storage.port import StorageError


class TestInMemoryCache:
    async def test_a_stored_value_comes_back(self) -> None:
        cache = InMemoryCache()

        await cache.set("greeting", "hello")

        assert await cache.get("greeting") == "hello"

    async def test_an_unknown_key_is_a_miss_not_an_error(self) -> None:
        cache = InMemoryCache()

        assert await cache.get("never-written") is None

    async def test_hits_and_misses_are_counted_separately(self) -> None:
        """`/ready` reports cache health, so these counters are user-visible."""
        cache = InMemoryCache()
        await cache.set("k", "v")

        await cache.get("k")
        await cache.get("k")
        await cache.get("absent")

        assert (cache.hits, cache.misses) == (2, 1)

    async def test_an_expired_entry_is_gone_rather_than_stale(self) -> None:
        cache = InMemoryCache()
        await cache.set("k", "v", ttl_seconds=0)

        assert await cache.get("k") is None
        assert not await cache.exists("k")

    async def test_an_unexpired_entry_is_returned(self) -> None:
        cache = InMemoryCache()
        await cache.set("k", "v", ttl_seconds=60)

        assert await cache.get("k") == "v"
        assert await cache.exists("k")

    async def test_set_without_a_ttl_never_expires(self) -> None:
        cache = InMemoryCache()
        await cache.set("k", "v")

        assert cache._store["k"].expires_at is None
        assert await cache.get("k") == "v"

    async def test_delete_reports_how_many_keys_it_actually_removed(self) -> None:
        cache = InMemoryCache()
        await cache.set("a", "1")
        await cache.set("b", "2")

        assert await cache.delete("a", "b", "never-existed") == 2

    async def test_increment_starts_a_counter_and_then_accumulates(self) -> None:
        cache = InMemoryCache()

        assert await cache.increment("streak") == 1
        assert await cache.increment("streak") == 2
        assert await cache.increment("streak", amount=4) == 6

    async def test_an_expired_counter_restarts_rather_than_adding_to_a_stale_value(
        self,
    ) -> None:
        cache = InMemoryCache()
        await cache.increment("streak")
        await cache.set("streak", "9", ttl_seconds=0)

        assert await cache.increment("streak") == 1

    async def test_concurrent_increments_do_not_lose_writes(self) -> None:
        """The lock is the whole point of this adapter; without it this test fails."""
        cache = InMemoryCache()

        await asyncio.gather(*(cache.increment("counter") for _ in range(50)))

        assert await cache.get("counter") == "50"

    def test_build_key_namespaces_and_skips_empty_parts(self) -> None:
        cache = InMemoryCache(namespace="test-ns")

        assert cache.build_key("level", "A1") == "test-ns:level:A1"
        assert cache.build_key("level", "", "A1") == "test-ns:level:A1"

    async def test_clear_empties_the_store(self) -> None:
        cache = InMemoryCache()
        await cache.set("k", "v")

        cache.clear()

        assert await cache.get("k") is None

    async def test_ping_is_always_true(self) -> None:
        assert await InMemoryCache().ping() is True


class TestInMemoryQueue:
    async def test_a_job_comes_back_in_order(self) -> None:
        queue = InMemoryQueue()
        first, second = Job(name=JobName.PROCESS_ANALYTICS), Job(name=JobName.AI_TASK)
        await queue.enqueue(first)
        await queue.enqueue(second)

        assert await queue.dequeue() is first
        assert await queue.dequeue() is second

    async def test_dequeue_returns_none_when_nothing_arrives_in_time(self) -> None:
        queue = InMemoryQueue()

        assert await queue.dequeue(timeout_ms=10) is None

    async def test_a_dedupe_key_is_accepted_once_while_queued(self) -> None:
        """Enqueueing the same logical work twice must not run it twice."""
        queue = InMemoryQueue()
        job = Job(name=JobName.SEND_NOTIFICATION, dedupe_key="user-7")

        assert await queue.enqueue(job) is True
        assert await queue.enqueue(job) is False
        assert await queue.size() == 1

    async def test_a_dedupe_key_is_reusable_once_its_job_is_dequeued(self) -> None:
        """Suppressing forever would turn a retry into a silent no-op."""
        queue = InMemoryQueue()
        job = Job(name=JobName.SEND_NOTIFICATION, dedupe_key="user-7")
        await queue.enqueue(job)
        await queue.dequeue()

        assert await queue.enqueue(job) is True

    async def test_jobs_without_a_dedupe_key_are_never_suppressed(self) -> None:
        queue = InMemoryQueue()

        assert await queue.enqueue(Job(name=JobName.AI_TASK)) is True
        assert await queue.enqueue(Job(name=JobName.AI_TASK)) is True

    async def test_drain_runs_every_queued_job_through_the_handler(self) -> None:
        queue = InMemoryQueue()
        seen: list[Job] = []
        await queue.enqueue(Job(name=JobName.PROCESS_ANALYTICS))
        await queue.enqueue(Job(name=JobName.AI_TASK))

        async def handler(job: Job) -> None:
            seen.append(job)

        assert await queue.drain(handler) == 2
        assert len(seen) == 2
        assert await queue.size() == 0

    async def test_drain_survives_a_failing_handler_and_still_drains(self) -> None:
        """One bad job must not wedge the queue behind it."""
        queue = InMemoryQueue()
        completed: list[Job] = []
        await queue.enqueue(Job(name=JobName.AI_TASK))
        await queue.enqueue(Job(name=JobName.PROCESS_ANALYTICS))

        async def handler(job: Job) -> None:
            if job.name is JobName.AI_TASK:
                raise RuntimeError("boom")
            completed.append(job)

        assert await queue.drain(handler) == 2
        assert len(completed) == 1

    async def test_drain_honours_its_limit_and_leaves_the_rest(self) -> None:
        queue = InMemoryQueue()
        for _ in range(5):
            await queue.enqueue(Job(name=JobName.AI_TASK))

        async def handler(job: Job) -> None:
            return None

        assert await queue.drain(handler, limit=2) == 2
        assert await queue.size() == 3

    async def test_close_discards_everything_still_queued(self) -> None:
        queue = InMemoryQueue()
        await queue.enqueue(Job(name=JobName.AI_TASK))

        await queue.close()

        assert await queue.size() == 0

    async def test_ping_is_always_true(self) -> None:
        assert await InMemoryQueue().ping() is True


class TestLocalObjectStorage:
    @pytest.fixture
    def storage(self, tmp_path: Path) -> LocalObjectStorage:
        return LocalObjectStorage(root=tmp_path / "store")

    async def test_a_stored_object_comes_back_with_its_checksum(
        self, storage: LocalObjectStorage
    ) -> None:
        stored = await storage.put("audio/lesson-1.mp3", b"ID3fake", content_type="audio/mpeg")

        assert await storage.get("audio/lesson-1.mp3") == b"ID3fake"
        assert stored.size_bytes == 7
        assert stored.content_type == "audio/mpeg"
        assert stored.checksum_sha256 == (
            "a674979b07be3009b1622c7cf93bcf0506c33e236b58077e8e897399f3225959"
        )

    async def test_a_stored_object_can_be_confirmed_present(
        self, storage: LocalObjectStorage
    ) -> None:
        await storage.put("a.txt", b"x", content_type="text/plain")

        assert await storage.exists("a.txt") is True
        assert await storage.exists("b.txt") is False

    async def test_a_missing_object_raises_rather_than_returning_empty(
        self, storage: LocalObjectStorage
    ) -> None:
        """Empty bytes and absent bytes must never be confused."""
        await storage.put("empty.txt", b"", content_type="text/plain")

        assert await storage.get("empty.txt") == b""
        with pytest.raises(StorageError, match="Object not found"):
            await storage.get("never-written.txt")

    async def test_streaming_reassembles_to_the_original_bytes(
        self, storage: LocalObjectStorage
    ) -> None:
        payload = bytes(range(256)) * 8
        await storage.put("big.bin", payload, content_type="application/octet-stream")

        chunks = [chunk async for chunk in storage.stream("big.bin", chunk_size=100)]

        assert b"".join(chunks) == payload

    async def test_delete_removes_the_object_and_its_sidecar(
        self, storage: LocalObjectStorage
    ) -> None:
        await storage.put("a.mp3", b"x", content_type="audio/mpeg")
        assert (storage._path("a.mp3.meta")).is_file(), "content type is recorded beside the object"

        assert await storage.delete("a.mp3") is True
        assert await storage.exists("a.mp3") is False
        assert not (storage._path("a.mp3.meta")).exists()

    async def test_deleting_something_absent_reports_false(
        self, storage: LocalObjectStorage
    ) -> None:
        assert await storage.delete("never-written.txt") is False

    async def test_a_traversing_key_is_refused(self, storage: LocalObjectStorage) -> None:
        with pytest.raises(StorageError, match="outside storage root"):
            await storage.put("../escape.txt", b"x", content_type="text/plain")

    async def test_a_traversing_key_cannot_read_a_sibling_directory_that_shares_its_prefix(
        self, storage: LocalObjectStorage, tmp_path: Path
    ) -> None:
        """The regression that matters here.

        Guarding a path with ``str.startswith(root)`` looks correct and is not: a sibling
        directory whose name merely begins with the root's name satisfies that prefix test
        while living somewhere else entirely. So with a root of ``store``, a key of
        ``../store-evil/secret.txt`` used to pass the check and read outside the root.

        Asserting the sibling case specifically, because the plain ``../`` case is caught
        by almost any guard and gives false confidence that this class of check works.
        """
        sibling = tmp_path / "store-evil"
        sibling.mkdir()
        (sibling / "secret.txt").write_bytes(b"someone else's data")

        with pytest.raises(StorageError, match="outside storage root"):
            await storage.get("../store-evil/secret.txt")

    async def test_an_absolute_key_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        root.mkdir()
        outsider = tmp_path / "outside.txt"
        outsider.write_bytes(b"secret")
        storage = LocalObjectStorage(root=root)

        with pytest.raises(StorageError, match="outside storage root"):
            await storage.get(str(outsider))

    async def test_presigned_urls_are_local_and_carry_the_configured_ttl(self) -> None:
        storage = LocalObjectStorage()

        download = await storage.presigned_url("audio/lesson-1.mp3", expires_in_seconds=90)

        assert download.url == "local://audio/lesson-1.mp3"
        assert download.expires_in_seconds == 90

    async def test_presigned_urls_fall_back_to_the_configured_ttl(self) -> None:
        download = await LocalObjectStorage().presigned_url("a.txt")

        assert download.expires_in_seconds > 0

    async def test_a_presigned_upload_names_the_key_and_binds_the_content_type(self) -> None:
        """The write grant is distinct from the read one, and states its requirement.

        With the filesystem adapter there is nothing to actually sign, but the contract a
        client codes against must not change between local and S3: the key is named, and the
        ``Content-Type`` the PUT must send is returned up front.
        """
        storage = LocalObjectStorage()

        grant = await storage.presigned_upload(
            "avatar/abc.png", content_type="image/png", expires_in_seconds=90
        )

        assert grant.url == "local://upload/avatar/abc.png"
        assert grant.expires_in_seconds == 90
        assert grant.required_headers == {"Content-Type": "image/png"}

    async def test_a_presigned_upload_falls_back_to_the_configured_ttl(self) -> None:
        grant = await LocalObjectStorage().presigned_upload("a.txt", content_type="text/plain")

        assert grant.expires_in_seconds > 0
