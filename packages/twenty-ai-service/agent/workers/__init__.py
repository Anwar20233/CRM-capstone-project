"""Agent workers — reusable tool-calling loops specialised by scope."""

from agent.workers.base_worker import BaseWorker

__all__ = ["BaseWorker", "ReaderWorker", "WriterWorker"]


def __getattr__(name: str):
    if name == "ReaderWorker":
        from agent.workers.reader_worker import ReaderWorker

        return ReaderWorker
    if name == "WriterWorker":
        from agent.workers.writer_worker import WriterWorker

        return WriterWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
