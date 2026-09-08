"""Non-blocking logging transport used by the desktop application.

The ticket runner is deliberately kept away from both Qt rendering and disk
I/O.  Producer threads only append a :class:`logging.LogRecord` to an
unbounded in-memory queue.  A dedicated listener formats/redacts records,
writes the rotating file and publishes a batched GUI update.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ticket_app.logging_utils import RedactingFormatter, make_rotating_file_handler


GuiLogLine = tuple[str, str]
GuiBatchSink = Callable[[Sequence[GuiLogLine]], None]
GuiLineSink = Callable[[str, str], None]


@dataclass(frozen=True)
class _QueuedRecord:
    record: logging.LogRecord
    sensitive_terms: tuple[str, ...]


@dataclass(frozen=True)
class _Configure:
    file_path: Optional[Path]


@dataclass(frozen=True)
class _Flush:
    completed: threading.Event


@dataclass(frozen=True)
class _Shutdown:
    completed: threading.Event


_QueueItem = _QueuedRecord | _Configure | _Flush | _Shutdown


def _normalise_terms(terms: Iterable[str]) -> tuple[str, ...]:
    """Return stable, non-empty terms without retaining a mutable input."""

    return tuple(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))


class AsyncQueueLogHandler(logging.Handler):
    """A logging handler whose ``emit`` path never waits for a consumer.

    ``queue.SimpleQueue`` is intentionally unbounded: an unusually slow disk
    or GUI must not feed back into timing-sensitive ticket requests.  The
    listener batches GUI delivery, so a burst still produces at most one GUI
    callback per configured interval.
    """

    def __init__(self, output_queue: "queue.SimpleQueue[_QueueItem]", level: int) -> None:
        super().__init__(logging.NOTSET)
        self._output_queue = output_queue
        self._settings_lock = threading.Lock()
        self._minimum_level = int(level)
        self._sensitive_terms: tuple[str, ...] = ()
        self._accepting = True

    @property
    def minimum_level(self) -> int:
        with self._settings_lock:
            return self._minimum_level

    def configure(self, level: int, sensitive_terms: Iterable[str]) -> None:
        """Atomically switch the settings snapshotted by future records."""

        normalised_terms = _normalise_terms(sensitive_terms)
        with self._settings_lock:
            self._minimum_level = int(level)
            self._sensitive_terms = normalised_terms

    def stop_accepting(self) -> None:
        with self._settings_lock:
            self._accepting = False

    def emit(self, record: logging.LogRecord) -> None:
        # Do not call getMessage(), format(), file APIs or Qt here.  Keeping the
        # lock scope tiny also makes reconfiguration deterministic without
        # turning the logging path into a wait for the listener.
        with self._settings_lock:
            if not self._accepting or record.levelno < self._minimum_level:
                return
            terms = self._sensitive_terms
        self._output_queue.put(_QueuedRecord(record, terms))


class AsyncLogPipeline:
    """Own an asynchronous GUI/file logging listener and its queue handler.

    Attach :attr:`handler` to the desired logger.  ``configure_task`` may be
    called before every run to update its level, passenger-name redaction and
    rotating-file destination.  ``close`` drains all records accepted before
    shutdown and is therefore the method the main window should call after it
    removes the handler from the root logger.
    """

    def __init__(
        self,
        *,
        gui_batch_sink: Optional[GuiBatchSink] = None,
        gui_line_sink: Optional[GuiLineSink] = None,
        level: int = logging.DEBUG,
        batch_interval: float = 0.1,
        thread_name: str = "12306FairTicket-log-listener",
    ) -> None:
        if gui_batch_sink is not None and gui_line_sink is not None:
            raise ValueError("gui_batch_sink and gui_line_sink are mutually exclusive")
        if batch_interval < 0:
            raise ValueError("batch_interval must not be negative")

        self._queue: "queue.SimpleQueue[_QueueItem]" = queue.SimpleQueue()
        self._gui_batch_sink = gui_batch_sink
        self._gui_line_sink = gui_line_sink
        self._batch_interval = float(batch_interval)
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self.handler = AsyncQueueLogHandler(self._queue, level)
        self._thread = threading.Thread(target=self._listen, name=thread_name, daemon=True)
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def configure_task(
        self,
        level: int,
        sensitive_terms: Iterable[str] = (),
        file_path: Optional[Path] = None,
    ) -> None:
        """Queue a task configuration without opening files on the caller.

        The configuration marker and producer settings are changed under the
        same lock.  Records accepted before the marker retain their previous
        redaction snapshot; records after it use the new terms.
        """

        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("the asynchronous logging pipeline is closed")
            self._queue.put(_Configure(Path(file_path) if file_path is not None else None))
            self.handler.configure(level, sensitive_terms)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until all records currently ahead of the barrier are durable."""

        completed = threading.Event()
        with self._lifecycle_lock:
            if self._closed:
                return not self._thread.is_alive()
            self._queue.put(_Flush(completed))
        return completed.wait(max(0.0, timeout))

    def close(self, timeout: float = 5.0) -> bool:
        """Stop accepting records, drain the queue and close the log file."""

        completed = threading.Event()
        with self._lifecycle_lock:
            if self._closed:
                return not self._thread.is_alive()
            self._closed = True
            self.handler.stop_accepting()
            self._queue.put(_Shutdown(completed))
        finished = completed.wait(max(0.0, timeout))
        if finished:
            self._thread.join(timeout=max(0.0, timeout))
        return finished and not self._thread.is_alive()

    def _listen(self) -> None:
        file_handler: Optional[logging.Handler] = None
        pending_gui: list[GuiLogLine] = []
        last_gui_delivery = time.monotonic()

        def deliver_gui() -> None:
            nonlocal last_gui_delivery
            if not pending_gui:
                last_gui_delivery = time.monotonic()
                return
            batch = tuple(pending_gui)
            pending_gui.clear()
            try:
                if self._gui_batch_sink is not None:
                    self._gui_batch_sink(batch)
                elif self._gui_line_sink is not None:
                    for line, level_name in batch:
                        self._gui_line_sink(line, level_name)
            except Exception:
                # Never recursively log a sink failure back into this queue.
                pass
            last_gui_delivery = time.monotonic()

        def flush_file() -> None:
            if file_handler is not None:
                try:
                    file_handler.flush()
                except Exception:
                    pass

        def close_file() -> None:
            nonlocal file_handler
            if file_handler is None:
                return
            flush_file()
            try:
                file_handler.close()
            finally:
                file_handler = None

        while True:
            elapsed = time.monotonic() - last_gui_delivery
            timeout = max(0.0, self._batch_interval - elapsed)
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                deliver_gui()
                continue

            if isinstance(item, _QueuedRecord):
                record = item.record
                try:
                    gui_formatter = RedactingFormatter(
                        "%(asctime)s  %(levelname)s  %(message)s",
                        "%H:%M:%S",
                        sensitive_terms=item.sensitive_terms,
                    )
                    pending_gui.append((gui_formatter.format(record), record.levelname))
                    if file_handler is not None:
                        file_handler.setFormatter(
                            RedactingFormatter(
                                "%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S",
                                sensitive_terms=item.sensitive_terms,
                            )
                        )
                        file_handler.handle(record)
                except Exception:
                    # A malformed third-party LogRecord must not terminate the
                    # listener and silently disable all later logging.
                    pass

                if self._batch_interval == 0 or time.monotonic() - last_gui_delivery >= self._batch_interval:
                    deliver_gui()
                continue

            if isinstance(item, _Configure):
                close_file()
                if item.file_path is not None:
                    try:
                        file_handler = make_rotating_file_handler(item.file_path, logging.NOTSET)
                    except OSError as exc:
                        pending_gui.append(
                            (
                                f"{time.strftime('%H:%M:%S')}  ERROR  无法创建本地轮转日志: {exc}",
                                "ERROR",
                            )
                        )
                continue

            if isinstance(item, _Flush):
                deliver_gui()
                flush_file()
                item.completed.set()
                continue

            if isinstance(item, _Shutdown):
                deliver_gui()
                close_file()
                item.completed.set()
                return


__all__ = [
    "AsyncLogPipeline",
    "AsyncQueueLogHandler",
    "GuiBatchSink",
    "GuiLineSink",
    "GuiLogLine",
]
