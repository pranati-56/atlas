"""The worker process:  python -m atlas.jobs.worker

Claims jobs, runs them with a renewing lease, and marks them done or failed.
Runs N concurrently in one event loop — the work is almost entirely waiting on
Postgres and provider APIs, so threads or processes would buy nothing here.

Shut down with SIGINT/SIGTERM: in-flight jobs are released immediately rather
than being left to time out, so a deploy does not strand work for a lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

from atlas.config import settings
from atlas.db import close_pool
from atlas.jobs import queue
from atlas.jobs.handlers import HANDLERS, NonRetryable
from atlas.jobs.queue import Job

log = logging.getLogger("atlas.worker")

#: How often to renew a running job's lease. A third of the lease gives two
#: chances to miss a beat before another worker reclaims the job.
HEARTBEAT_DIVISOR = 3
#: How often to sweep for jobs whose worker died mid-flight.
RECLAIM_INTERVAL_S = 30


def worker_id() -> str:
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self, name: str | None = None) -> None:
        cfg = settings()
        self.name = name or worker_id()
        self.concurrency = cfg.worker_concurrency
        self.poll_s = cfg.worker_poll_ms / 1000
        self.lease_s = cfg.job_lease_seconds
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stopping.set()

    async def _heartbeat(self, job: Job) -> None:
        interval = max(5.0, self.lease_s / HEARTBEAT_DIVISOR)
        while True:
            await asyncio.sleep(interval)
            if not await queue.heartbeat(job.id, self.name, self.lease_s):
                # The lease was already reclaimed — another worker may be
                # running this job now. Stop renewing; whichever copy finishes
                # first decides the outcome.
                log.warning("job %d lost its lease", job.id)
                return

    async def _run_job(self, job: Job) -> None:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            await queue.fail(job.id, f"no handler registered for kind {job.kind!r}")
            return

        beat = asyncio.create_task(self._heartbeat(job))
        try:
            await handler(job.payload)
            await queue.complete(job.id)
        except asyncio.CancelledError:
            # Shutdown mid-job: release it immediately rather than making the
            # next worker wait out the lease.
            await queue.fail(job.id, "worker shut down mid-job")
            raise
        except NonRetryable as exc:
            # Burn the remaining attempts in one go: retrying cannot help, and a
            # poison job should not occupy the queue through an hour of backoff.
            log.info("job %d (%s) rejected: %s", job.id, job.kind, exc)
            for _ in range(job.max_attempts - job.attempts + 1):
                if await queue.fail(job.id, str(exc)) == "dead":
                    break
        except Exception as exc:  # noqa: BLE001 — classified by fail_job
            status = await queue.fail(job.id, str(exc))
            log.exception(
                "job %d (%s) failed, attempt %d/%d -> %s",
                job.id,
                job.kind,
                job.attempts,
                job.max_attempts,
                status,
            )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _reclaimer(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), RECLAIM_INTERVAL_S)
            if self._stopping.is_set():
                return
            try:
                n = await queue.reclaim_expired()
                if n:
                    log.warning("reclaimed %d job(s) from dead workers", n)
            except Exception:  # noqa: BLE001 — a sweep failure must not exit
                log.exception("reclaim sweep failed")

    async def run(self) -> None:
        log.info(
            "worker %s starting (concurrency=%d, lease=%ds)",
            self.name,
            self.concurrency,
            self.lease_s,
        )
        reclaimer = asyncio.create_task(self._reclaimer())

        try:
            while not self._stopping.is_set():
                free = self.concurrency - len(self._running)
                if free <= 0:
                    await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
                    continue

                try:
                    jobs = await queue.claim(self.name, free, self.lease_s)
                except Exception:  # noqa: BLE001 — a DB blip must not exit
                    log.exception("claim failed")
                    await asyncio.sleep(self.poll_s)
                    continue

                if not jobs:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), self.poll_s)
                    continue

                for job in jobs:
                    task = asyncio.create_task(self._run_job(job))
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
        finally:
            reclaimer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaimer
            if self._running:
                log.info("releasing %d in-flight job(s)", len(self._running))
                for task in list(self._running):
                    task.cancel()
                await asyncio.gather(*self._running, return_exceptions=True)
            log.info("worker %s stopped", self.name)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    worker = Worker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks SIGTERM
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run()
    finally:
        await close_pool()


def run() -> None:
    """Console entrypoint: `uv run atlas-worker`.

    Ctrl-C is how a worker is normally stopped, not a crash — the signal
    handlers above have already released whatever was in flight by the time it
    reaches here.
    """
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
