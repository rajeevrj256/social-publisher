"""RQ worker entry point."""

from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from .config import get_settings
from .jobs import recover_stuck_publications
from .logging_setup import setup_logging

QUEUE_NAME = "publish"


def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_redis())


def main() -> None:
    log = setup_logging("worker")
    # Anything the previous process left mid-flight is reconciled before new
    # work is accepted, so recovery cannot race a fresh publish of the same row.
    recovered = recover_stuck_publications()
    if recovered:
        log.info("recovered %s stuck publications", recovered)
    log.info("worker ready on queue '%s'", QUEUE_NAME)
    Worker([QUEUE_NAME], connection=get_redis()).work(with_scheduler=False)


if __name__ == "__main__":
    main()
