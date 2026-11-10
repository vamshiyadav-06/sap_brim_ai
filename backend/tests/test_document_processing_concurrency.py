"""Concurrency regressions for document processing state and SSE progress fan-out."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from backend.app.services.document_processing_service import DocumentProcessingService


def test_already_running_start_returns_snapshot_without_reacquiring_lock(monkeypatch):
    service = DocumentProcessingService()
    expected = {"is_processing": True, "current_stage": "Creating embeddings..."}
    with service._lock:
        service._is_processing = True
        service._progress_state.update(expected)

    def should_not_reacquire():
        raise AssertionError("start_processing must not call get_progress_status under _lock")

    monkeypatch.setattr(service, "get_progress_status", should_not_reacquire)
    result_holder = []
    caller = threading.Thread(target=lambda: result_holder.append(service.start_processing()), daemon=True)
    caller.start()
    caller.join(timeout=1)

    assert not caller.is_alive(), "already-running response deadlocked on the progress lock"
    result = result_holder[0]
    assert result["status"] == "already_running"
    assert all(result["progress"][key] == value for key, value in expected.items())


def test_progress_status_remains_available_during_concurrent_broadcasts():
    service = DocumentProcessingService()

    def publish_updates():
        for index in range(500):
            service._broadcast({"overall_progress_pct": index % 101})

    with ThreadPoolExecutor(max_workers=2) as executor:
        publisher = executor.submit(publish_updates)
        readers = [executor.submit(service.get_progress_status) for _ in range(100)]
        publisher.result(timeout=2)
        snapshots = [reader.result(timeout=2) for reader in readers]

    assert all("current_stage" in snapshot for snapshot in snapshots)


def test_worker_broadcast_delivers_on_subscriber_loop_and_disconnects_cleanly():
    async def exercise_subscriber():
        service = DocumentProcessingService()
        loop = asyncio.get_running_loop()
        queue = await asyncio.to_thread(service.subscribe, loop)

        worker = threading.Thread(
            target=service._broadcast,
            args=({"current_stage": "Creating embeddings..."},),
        )
        worker.start()
        await asyncio.to_thread(worker.join, 1)

        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event["current_stage"] == "Creating embeddings..."
        await asyncio.to_thread(service.unsubscribe, queue)
        with service._lock:
            assert queue not in service._listeners

    asyncio.run(exercise_subscriber())


def test_slow_subscriber_receives_latest_snapshot_without_queue_growth():
    async def exercise_subscriber():
        service = DocumentProcessingService()
        queue = service.subscribe()

        worker = threading.Thread(
            target=lambda: [
                service._broadcast({"overall_progress_pct": progress})
                for progress in (10, 20, 30)
            ]
        )
        worker.start()
        await asyncio.to_thread(worker.join, 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event["overall_progress_pct"] == 30
        assert queue.empty()
        await asyncio.to_thread(service.unsubscribe, queue)

    asyncio.run(exercise_subscriber())
