import email.utils
import logging
import time
from datetime import datetime
from typing import Dict, List

import requests

from .configuration import AppConfig, AppError, BASE_URL
from .runtime import CancellationToken, EventSink, RunCancelled, emit_event


class ServerClock:
    def __init__(self, session: requests.Session, cfg: AppConfig) -> None:
        self.session = session
        self.cfg = cfg
        self.timeout = cfg.request_timeout_seconds
        self.offset_seconds = 0.0
        self._base_server_timestamp = time.time()
        self._base_perf_counter = time.perf_counter()
        self.has_synchronized = False

    def sync(
        self,
        cancel_token: CancellationToken | None = None,
        event_sink: EventSink = None,
        *,
        budget_seconds: float | None = None,
        source: str = "startup",
        deadline_monotonic: float | None = None,
    ) -> bool:
        deadline = deadline_monotonic
        if budget_seconds is not None:
            budget_deadline = time.monotonic() + max(0.0, budget_seconds)
            deadline = min(deadline, budget_deadline) if deadline is not None else budget_deadline
        samples: List[Dict[str, float]] = []
        errors: List[str] = []
        for index in range(self.cfg.time_sync_samples):
            if cancel_token is not None:
                cancel_token.checkpoint()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                started_wall = time.time()
                started_perf = time.perf_counter()
                if remaining is None:
                    response = self.session.head(f"{BASE_URL}/otn/leftTicket/init", timeout=self.timeout)
                else:
                    # Split the remaining budget between connect/read. Requests
                    # timeouts remain cooperative network bounds, not a hard
                    # real-time guarantee (e.g. OS DNS resolution can overrun).
                    timeout = min(self.timeout, max(0.001, remaining / 2))
                    response = self.session.head(f"{BASE_URL}/otn/leftTicket/init",
                                                 timeout=timeout, allow_redirects=False)
                ended_perf = time.perf_counter()
                ended_wall = time.time()
                if cancel_token is not None:
                    cancel_token.checkpoint()
                status = getattr(response, "status_code", 200)
                if isinstance(status, int) and not 200 <= status < 300:
                    raise AppError("校时接口 HTTP 状态异常")
                server_date = response.headers.get("Date")
                if not server_date:
                    raise AppError("12306 响应没有 Date 头")
                rtt = ended_perf - started_perf
                server_dt = email.utils.parsedate_to_datetime(server_date)
                estimated_server_ts = server_dt.timestamp() + rtt / 2
                offset = estimated_server_ts - ended_wall
                samples.append(
                    {
                        "index": float(index + 1),
                        "rtt": rtt,
                        "offset": offset,
                        "base_server_timestamp": estimated_server_ts,
                        "base_perf_counter": ended_perf,
                        "started_wall": started_wall,
                        "checked_at": ended_wall,
                    }
                )
            except RunCancelled:
                raise
            except Exception as exc:
                errors.append(type(exc).__name__)
            if index < self.cfg.time_sync_samples - 1:
                delay = 0.05 if deadline is None else min(0.05, max(0.0, deadline - time.monotonic()))
                if cancel_token is None:
                    time.sleep(delay)
                else:
                    cancel_token.wait(delay)

        if cancel_token is not None:
            cancel_token.checkpoint()

        if not samples:
            checked_at = time.time()
            if not self.has_synchronized:
                self.offset_seconds = 0.0
                self._base_server_timestamp = checked_at
                self._base_perf_counter = time.perf_counter()
            fallback = "保留上次成功校时结果" if self.has_synchronized else "使用本地时间"
            logging.warning("同步 12306 服务器时间失败，%s: %s", fallback, "; ".join(errors) or "已达到校时预算")
            emit_event(
                event_sink,
                "clock_sync",
                f"服务器校时失败，{fallback}",
                offset_seconds=self.offset_seconds,
                rtt_ms=None,
                server_timestamp=self._base_server_timestamp,
                monotonic_timestamp=self._base_perf_counter,
                success=False,
                source=source,
                checked_at=checked_at,
            )
            return False

        eligible = [sample for sample in samples if sample["rtt"] <= self.cfg.time_sync_max_rtt_seconds]
        selected_pool = eligible or samples
        best = min(selected_pool, key=lambda sample: sample["rtt"])
        self.offset_seconds = best["offset"]
        self._base_server_timestamp = best["base_server_timestamp"]
        self._base_perf_counter = best["base_perf_counter"]
        self.has_synchronized = True

        rtts = ", ".join(f"{sample['rtt'] * 1000:.0f}ms" for sample in samples)
        ignored = len(samples) - len(eligible)
        if eligible:
            logging.info(
                "已同步 12306 服务器时间，偏移 %.3f 秒，采用样本 #%d，RTT %.0fms；全部 RTT: %s",
                self.offset_seconds,
                int(best["index"]),
                best["rtt"] * 1000,
                rtts,
            )
        else:
            logging.info(
                "已同步 12306 服务器时间，偏移 %.3f 秒，所有样本 RTT 均超过阈值，采用最低 RTT 样本 #%d %.0fms；全部 RTT: %s",
                self.offset_seconds,
                int(best["index"]),
                best["rtt"] * 1000,
                rtts,
            )
        if ignored:
            logging.debug("时间同步忽略 %s 个 RTT 超过阈值的样本", ignored)
        emit_event(
            event_sink,
            "clock_sync",
            "服务器时间同步完成",
            offset_seconds=self.offset_seconds,
            rtt_ms=best["rtt"] * 1000,
            server_timestamp=self._base_server_timestamp,
            monotonic_timestamp=self._base_perf_counter,
            success=True,
            source=source,
            checked_at=best["checked_at"],
        )
        return True

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.now_timestamp())

    def now_timestamp(self) -> float:
        return self._base_server_timestamp + (time.perf_counter() - self._base_perf_counter)

    def sleep_until(self, target: datetime, cancel_token: CancellationToken | None = None) -> None:
        target_timestamp = target.timestamp()
        while True:
            remaining = target_timestamp - self.now_timestamp()
            if remaining <= 0:
                return
            if remaining > 1:
                duration = min(remaining - 0.5, 1.0)
            elif remaining > 0.1:
                duration = 0.02
            else:
                duration = 0.003
            if cancel_token is None:
                time.sleep(duration)
            else:
                cancel_token.wait(duration)
