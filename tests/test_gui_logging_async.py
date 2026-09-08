"""Offline behavioural tests for the GUI asynchronous log transport."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ticket_app.gui.async_logging import AsyncLogPipeline


def _logger_with(pipeline: AsyncLogPipeline) -> logging.Logger:
    logger = logging.getLogger(f"test.gui.async.{id(pipeline)}")
    logger.handlers[:] = [pipeline.handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_pipeline_redacts_writes_and_flushes(tmp_path: Path) -> None:
    batches: list[tuple[tuple[str, str], ...]] = []
    pipeline = AsyncLogPipeline(gui_batch_sink=lambda lines: batches.append(tuple(lines)), batch_interval=0)
    logger = _logger_with(pipeline)
    log_path = tmp_path / "logs" / "app.log"
    try:
        pipeline.configure_task(logging.INFO, ["张三"], log_path)
        logger.debug("this debug message is filtered")
        logger.info("张三 token=very-secret 已开始查询")

        assert pipeline.flush()
        delivered = [line for batch in batches for line, _level in batch]
        assert any("<姓名已隐藏>" in line for line in delivered)
        assert all("very-secret" not in line for line in delivered)
        assert all("debug message" not in line for line in delivered)

        contents = log_path.read_text(encoding="utf-8")
        assert "<姓名已隐藏>" in contents
        assert "very-secret" not in contents
    finally:
        logger.removeHandler(pipeline.handler)
        assert pipeline.close()


def test_slow_gui_consumer_never_blocks_log_producer() -> None:
    sink_started = threading.Event()
    release_sink = threading.Event()

    def slow_sink(_lines: object) -> None:
        sink_started.set()
        release_sink.wait(2)

    pipeline = AsyncLogPipeline(gui_batch_sink=slow_sink, batch_interval=0)
    logger = _logger_with(pipeline)
    try:
        logger.info("trigger a deliberately blocked GUI consumer")
        assert sink_started.wait(1)

        start = time.monotonic()
        for number in range(3_000):
            logger.info("timing-sensitive request log %s", number)
        elapsed = time.monotonic() - start

        # The listener is intentionally stuck in the sink.  An unbounded
        # queue means producers only enqueue and are not paced by GUI work.
        assert elapsed < 0.8
    finally:
        release_sink.set()
        logger.removeHandler(pipeline.handler)
        assert pipeline.close(timeout=5)


def test_close_drains_records_before_listener_exits() -> None:
    delivered: list[str] = []
    pipeline = AsyncLogPipeline(
        gui_batch_sink=lambda lines: delivered.extend(line for line, _level in lines),
        batch_interval=60,
    )
    logger = _logger_with(pipeline)
    try:
        logger.warning("final record before shutdown")
        assert pipeline.close()
        assert not pipeline.is_alive
        assert any("final record before shutdown" in line for line in delivered)
    finally:
        logger.removeHandler(pipeline.handler)
