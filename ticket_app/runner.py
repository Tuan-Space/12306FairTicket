import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from .client import RailwayClient
from .clock import ServerClock
from .configuration import AppConfig, AppError, PreparedPassengerSet, ResponseFormatError, SEAT_SPECS, _elapsed_ms, _perf_log, _resolve_schedule_time
from .helpers import _build_passenger_strings, _is_terminal_order_failure, _resolve_submit_seat_code, _stock_available
from .preferences import OrderPreferencePayload, build_order_preference_payload
from .passengers import is_student_ticket_rejection, select_passengers
from .runtime import CancellationToken, EventSink, RunCancelled, emit_event
from .stations import StationStore
from .configuration import SHARED_BERTH_CODES


class _StopDeadlineReached(RunCancelled):
    """The run's fixed STOP_AT elapsed before an irreversible confirmation."""


class _DeadlineToken:
    """Preserve the caller's cancellation/actions while bounding safe waits."""

    def __init__(self, token: CancellationToken, remaining: Callable[[], Optional[float]]) -> None:
        self._token = token
        self._remaining = remaining

    def __getattr__(self, name: str) -> Any:
        return getattr(self._token, name)

    def checkpoint(self) -> None:
        self._token.checkpoint()
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            raise _StopDeadlineReached("已到停止时间")

    def _bounded_wait(self, seconds: float) -> float:
        remaining = self._remaining()
        return max(0.0, min(seconds, remaining)) if remaining is not None else max(0.0, seconds)

    def wait(self, seconds: float) -> None:
        self.checkpoint()
        self._token.wait(self._bounded_wait(seconds))
        self.checkpoint()

    def next_action(self, timeout: float = 0.0) -> Optional[str]:
        self.checkpoint()
        action = self._token.next_action(self._bounded_wait(timeout))
        self.checkpoint()
        return action


class TicketRunner:
    def __init__(
        self,
        cfg: AppConfig,
        event_sink: EventSink = None,
        cancel_token: CancellationToken | None = None,
        session: Optional[requests.Session] = None,
        clock: ServerClock | None = None,
    ) -> None:
        self.cfg = cfg
        self.event_sink = event_sink
        self.cancel_token = cancel_token or CancellationToken()
        self.client = RailwayClient(
            cfg,
            event_sink=event_sink,
            cancel_token=self.cancel_token,
            session=session,
        )
        self.clock = clock if clock is not None else ServerClock(self.client.session, cfg)
        # GUI operations share one session/worker slot. Reusing its in-memory
        # clock preserves the last successful anchor when a later sync fails.
        # Apply edited connection settings only on this owning worker thread.
        self.clock.session = self.client.session
        self.clock.cfg = cfg
        self.clock.timeout = cfg.request_timeout_seconds
        self.stations = StationStore(self.client.session, cfg)
        self._ambiguous_berth_warnings: set[tuple[str, str]] = set()
        self._order_state = "safe"

    @property
    def order_state(self) -> str:
        """Worker-owned order disposition, independent of queued UI events."""
        return getattr(self, "_order_state", "safe")

    @property
    def safe_to_restart(self) -> bool:
        return self.order_state == "safe"

    def _set_order_state(self, state: str) -> None:
        # A late cancellation or a failing UI callback must never erase proof
        # of an actual order. Unknown confirmations also block new attempts.
        if self.order_state != "success":
            self._order_state = state

    def run(self) -> int:
        try:
            result = self._run()
            emit_event(self.event_sink, "finished", "任务已完成", exit_code=result)
            return result
        except _StopDeadlineReached:
            logging.info("已到 STOP_AT，任务结束")
            self._phase("stopped", "已到停止时间")
            emit_event(self.event_sink, "finished", "已到停止时间", exit_code=1)
            return 1
        except RunCancelled:
            logging.warning("用户已停止任务")
            emit_event(self.event_sink, "phase", "任务已停止", phase="cancelled")
            emit_event(self.event_sink, "finished", "任务已停止", exit_code=130)
            return 130
        except Exception as exc:
            emit_event(self.event_sink, "error", str(exc), exception_type=type(exc).__name__)
            raise

    def _run(self) -> int:
        if not self.safe_to_restart:
            raise AppError("本次已有已提交或待核对的订单，不能再次启动；请到 12306 官方订单页核对")
        self._resolve_stop_deadline()
        if not isinstance(self.cancel_token, _DeadlineToken):
            self.cancel_token = _DeadlineToken(self.cancel_token, self._remaining_until_stop)
            self.client.cancel_token = self.cancel_token
        self._safe_checkpoint()
        self._phase("clock", "正在同步 12306 服务器时间")
        self.clock.sync(self.cancel_token, self.event_sink)
        self._safe_checkpoint()
        self._phase("stations", "正在加载车站信息")
        self.stations.load(self.cancel_token, self.event_sink)
        self._safe_checkpoint()
        self._resolve_query_routes()
        self._phase("login", "正在检查登录状态")
        self.client.ensure_login()
        self.cancel_token.checkpoint()
        self._last_login_check_monotonic = time.monotonic()
        selected_passengers = self._select_passengers() if self.cfg.auto_submit else []
        self._prepared_passengers = self._prepare_passengers_by_seat_code(selected_passengers) if self.cfg.auto_submit else {}
        target_start = self._resolve_target_start()
        self._wait_for_query_start(target_start)
        self._safe_checkpoint()
        prepared_passengers = self._prepared_passengers
        self._maintenance_available(False, clear_pending=True)
        self._phase("querying", "开始查询余票")
        cart = self.cfg.cart_items
        logging.info("购物车任务启动: 日期 %s，%s 个备选，%s 个站对；按购物车顺序尝试，成功一项即停止",
                     self.cfg.train_date, len(cart), len(self._query_routes))
        for index, item in enumerate(cart, 1):
            logging.info("备选 %s: %s → %s · %s · %s", index, item.from_station, item.to_station,
                         item.train_code if item.train_scope == "specific" else "不限车次", item.seat_type)
        self._last_query_request_at = None
        first_query_perf = time.perf_counter()
        for attempt in range(1, self.cfg.max_retries + 1):
            self.cancel_token.checkpoint()
            if self._should_stop():
                logging.info("已到 STOP_AT，任务结束")
                emit_event(self.event_sink, "phase", "已到停止时间", phase="stopped")
                return 1
            logging.info("第 %s/%s 轮查询余票...", attempt, self.cfg.max_retries)
            emit_event(
                self.event_sink,
                "query",
                f"第 {attempt}/{self.cfg.max_retries} 轮查询",
                attempt=attempt,
                max_retries=self.cfg.max_retries,
                interval_seconds=self._current_query_interval(target_start),
            )
            query_start = time.perf_counter()
            if attempt == 1:
                _perf_log(self.cfg, "等待结束到首个查票请求准备耗时 %.1fms", _elapsed_ms(first_query_perf))
            candidate_count = 0
            # This iterator is lazy: later station pairs are queried only
            # after earlier entries have actually been attempted.
            for candidate in self._round_candidates(target_start):
                candidate_count += 1
                self._safe_checkpoint()
                ticket = candidate["ticket"]
                candidate["found_perf"] = time.perf_counter()
                logging.info(
                    "发现票源: %s %s-%s %s %s 余票:%s",
                    ticket["station_train_code"],
                    ticket["start_time"],
                    ticket["arrive_time"],
                    candidate["seat_label"],
                    ticket["duration"],
                    candidate["stock"],
                )
                emit_event(
                    self.event_sink,
                    "candidate",
                    f"发现票源 {ticket['station_train_code']} {candidate['seat_label']}",
                    train=ticket["station_train_code"],
                    seat_label=candidate["seat_label"],
                    stock=candidate["stock"],
                    start_time=ticket["start_time"],
                    arrive_time=ticket["arrive_time"],
                    duration=ticket["duration"],
                    **self._candidate_context(candidate),
                )
                if not self.cfg.auto_submit:
                    continue
                if self._book_ticket(candidate, prepared_passengers):
                    return 0
                if not self.safe_to_restart:
                    raise AppError("订单状态尚未确认，整车任务已停止；请到 12306 官方订单页核对")
            _perf_log(self.cfg, "本轮查票总耗时 %.1fms，候选 %s 个", _elapsed_ms(query_start), candidate_count)
        logging.info("已达到最大重试次数，任务结束")
        emit_event(self.event_sink, "phase", "已达到最大重试次数", phase="stopped")
        return 1

    def _phase(self, phase: str, message: str, **data: Any) -> None:
        emit_event(self.event_sink, "phase", message, phase=phase, **data)

    def _sleep(self, seconds: float) -> None:
        self.cancel_token.wait(seconds)

    def _select_passengers(self) -> List[Dict[str, Any]]:
        self.cancel_token.checkpoint()
        passengers = self.client.get_passengers()
        selected = select_passengers(passengers, self.cfg.passenger_names,
                                     getattr(self.cfg, "passenger_ticket_types", None))
        logging.info("已选择乘车人: %s", "、".join(self.cfg.passenger_names))
        emit_event(
            self.event_sink,
            "passengers",
            "已加载常用乘车人",
            available_count=len(passengers),
        )
        return selected

    def _prepare_passengers_by_seat_code(self, passengers: List[Dict[str, Any]]) -> Dict[str, PreparedPassengerSet]:
        submit_codes = {spec.submit_code for spec in SEAT_SPECS.values() if spec.submit_code}
        submit_codes.update({"O", "1"})
        prepared: Dict[str, PreparedPassengerSet] = {}
        for submit_code in submit_codes:
            passenger_copies = []
            for passenger in passengers:
                passenger_copy = passenger.copy()
                passenger_copy["seat_type"] = submit_code
                passenger_copies.append(passenger_copy)
            passenger_ticket_str, old_passenger_str = _build_passenger_strings(passenger_copies)
            prepared[submit_code] = PreparedPassengerSet(passenger_copies, passenger_ticket_str, old_passenger_str)
        _perf_log(self.cfg, "已预生成 %s 种座席乘车人提交字符串", len(prepared))
        return prepared

    def _resolve_target_start(self) -> Optional[datetime]:
        return _resolve_schedule_time(self.cfg.start_at, self.clock.now())

    def _wait_for_query_start(self, target_start: Optional[datetime]) -> None:
        self._safe_checkpoint()
        if not target_start:
            return
        query_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        sync_at = min(target_start - timedelta(seconds=60), query_start - timedelta(seconds=10))
        login_at = min(target_start - timedelta(seconds=180), sync_at - timedelta(seconds=120))
        self._maintenance_target = target_start
        self._maintenance_query_start = query_start
        self._maintenance_sync_at = sync_at
        self._maintenance_login_at = login_at
        self._login_required = False
        self._final_sync_done = False
        now = self.clock.now()
        initial_check = getattr(self, "_last_login_check_monotonic", None)
        verified_at = now - timedelta(seconds=max(0.0, time.monotonic() - initial_check)) if initial_check is not None else None
        self._scheduled_login_done = verified_at is not None and verified_at >= login_at
        logging.info(
            "等待热身查询窗口: %s；目标开售时间: %s",
            query_start.strftime("%Y-%m-%d %H:%M:%S"),
            target_start.strftime("%Y-%m-%d %H:%M:%S"),
        )
        logging.info("本次登录复查时间 %s；最终校时时间 %s", login_at.strftime("%H:%M:%S"), sync_at.strftime("%H:%M:%S"))
        self._waiting_phase()
        self._maintenance_available(True)
        try:
            while True:
                self._safe_checkpoint()
                if (not self._login_required and self._scheduled_login_done and self._final_sync_done
                        and self.clock.now() >= query_start):
                    return
                # Drain an already requested action first; if its scheduled
                # counterpart is due, that same result satisfies it below.
                action = self.cancel_token.next_action(0)
                self._safe_checkpoint()
                if action is not None:
                    self._perform_maintenance(action)
                    continue
                now = self.clock.now()
                if not self._scheduled_login_done and now >= login_at:
                    self._scheduled_login_done = True
                    self._perform_maintenance("check_login", automatic=True)
                    continue
                if not self._login_required and not self._final_sync_done and now >= sync_at:
                    self._final_sync_done = True
                    self._perform_maintenance("sync_clock", automatic=True)
                    continue
                if not self._login_required and now >= query_start:
                    return
                deadlines = [query_start]
                if not self._scheduled_login_done:
                    deadlines.append(login_at)
                if not self._final_sync_done and not self._login_required:
                    deadlines.append(sync_at)
                if getattr(self, "_stop_deadline", None) is not None:
                    deadlines.append(self._stop_deadline)
                remaining = (min(deadlines) - now).total_seconds()
                if self._login_required:
                    timeout = 1.0
                elif remaining > 1:
                    timeout = min(remaining - 0.5, 1.0)
                elif remaining > 0.1:
                    timeout = 0.02
                else:
                    timeout = 0.003
                action = self.cancel_token.next_action(timeout)
                self._safe_checkpoint()
                if action is not None:
                    if (not self._login_required and self._scheduled_login_done and self._final_sync_done
                            and self.clock.now() >= query_start):
                        return
                    self._perform_maintenance(action)
        finally:
            self._maintenance_available(False, clear_pending=True)

    def _maintenance_available(self, enabled: bool, *, busy: bool = False, clear_pending: bool = False) -> None:
        interactive = bool(getattr(self.cancel_token, "supports_actions", False))
        self.cancel_token.set_actions_enabled(enabled and interactive and not busy, clear_pending=clear_pending)
        emit_event(self.event_sink, "maintenance_availability", enabled=enabled and interactive,
                   busy=busy, login_required=getattr(self, "_login_required", False))

    def _waiting_phase(self) -> None:
        self._phase(
            "waiting",
            "登录已失效，已暂停；请重新扫码后继续" if self._login_required else "等待进入热身查询窗口",
            target_timestamp=self._maintenance_target.timestamp(),
            query_start_timestamp=self._maintenance_query_start.timestamp(),
        )

    def _perform_maintenance(self, action: str, *, automatic: bool = False) -> None:
        self._safe_checkpoint()
        self._maintenance_available(True, busy=True)
        self.cancel_token.discard_action(action)
        try:
            if action == "check_login":
                coalescing_due = not self._scheduled_login_done and self.clock.now() >= self._maintenance_login_at
                if coalescing_due:
                    self._scheduled_login_done = True
                self._phase("login", "正在复查登录状态")
                try:
                    valid = self.client.check_session()
                except AppError:
                    # A network failure is not proof of expiry. A manual check
                    # leaves the previous state intact; the scheduled safeguard
                    # must fail visibly instead of silently assuming validity.
                    if automatic or coalescing_due:
                        raise AppError("开售前登录复查失败，无法确认会话；请重新开始任务重试") from None
                    return
                self._safe_checkpoint()
                if self.clock.now() >= self._maintenance_login_at:
                    # A check started just before the deadline may finish
                    # after it; its confirmed result serves both requests.
                    self._scheduled_login_done = True
                self._login_required = not valid
                if valid:
                    self._last_login_check_monotonic = time.monotonic()
                elif automatic:
                    self._login_during_wait()
            elif action == "login":
                self._login_during_wait()
            elif action == "sync_clock":
                if self.clock.now() >= self._maintenance_sync_at:
                    self._final_sync_done = True
                self._phase("clock", "正在同步 12306 服务器时间")
                remaining = (self._maintenance_query_start - self.clock.now()).total_seconds()
                budget = min(10.0, remaining) if remaining > 0 else 10.0
                until_stop = self._remaining_until_stop()
                if until_stop is not None:
                    budget = min(budget, max(0.0, until_stop))
                self.clock.sync(self.cancel_token, self.event_sink, budget_seconds=budget,
                                source="scheduled" if automatic else "manual")
                if self.clock.now() >= self._maintenance_sync_at:
                    self._final_sync_done = True
        finally:
            self._safe_checkpoint()
            self._waiting_phase()
            self._maintenance_available(True)

    def _login_during_wait(self) -> None:
        self._login_required = True
        self._phase("login", "登录已失效，请重新扫码")
        try:
            self.client.ensure_login(check_first=False)
        except RunCancelled:
            raise
        except AppError:
            if not getattr(self.cancel_token, "supports_actions", False):
                raise AppError("重新扫码未完成；请重新运行任务后扫码登录") from None
            logging.warning("重新扫码未完成，任务保持暂停；请点击重新扫码")
            return
        self._safe_checkpoint()
        # QR login can switch accounts: never retain the previous account's
        # passenger credentials or already generated submission strings.
        selected = self._select_passengers() if self.cfg.auto_submit else []
        self._prepared_passengers = self._prepare_passengers_by_seat_code(selected) if self.cfg.auto_submit else {}
        self._login_required = False
        # A replacement QR login can consume the entire remaining countdown;
        # calibrate again once it completes, even if an earlier sync ran.
        if self.clock.now() >= self._maintenance_sync_at:
            self._final_sync_done = False
        self._last_login_check_monotonic = time.monotonic()
        if self.clock.now() >= self._maintenance_login_at:
            self._scheduled_login_done = True

    def _resolve_stop_deadline(self) -> None:
        raw = getattr(self.cfg, "stop_at", "")
        self._stop_deadline = _resolve_schedule_time(raw, self.clock.now()) if raw else None
        self._stop_deadline_resolved = True

    def _remaining_until_stop(self) -> Optional[float]:
        # Once confirmation is sent, STOP_AT must not interrupt accounting for
        # its result. User cancellation is still handled as an unknown order.
        if not self.safe_to_restart:
            return None
        if not getattr(self, "_stop_deadline_resolved", False):
            self._resolve_stop_deadline()
        if self._stop_deadline is None:
            return None
        return (self._stop_deadline - self.clock.now()).total_seconds()

    def _should_stop(self) -> bool:
        remaining = self._remaining_until_stop()
        return remaining is not None and remaining <= 0

    def _safe_checkpoint(self) -> None:
        self.cancel_token.checkpoint()
        if self._should_stop():
            raise _StopDeadlineReached("已到停止时间")

    def _current_query_interval(self, target_start: Optional[datetime]) -> float:
        if not target_start:
            return self.cfg.query_interval_seconds
        now = self.clock.now()
        hot_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        hot_end = target_start + timedelta(seconds=self.cfg.hot_window_seconds)
        if hot_start <= now <= hot_end:
            return self.cfg.hot_query_interval_seconds
        return self.cfg.query_interval_seconds

    def _resolve_query_routes(self) -> None:
        if not self.cfg.cart_items:
            raise AppError("购物车为空，请先添加至少一个备选")
        station_pairs = [(item.from_station, item.to_station) for item in self.cfg.cart_items]
        self._query_routes = {
            pair: (self.stations.code(pair[0]), self.stations.code(pair[1]))
            for pair in dict.fromkeys(station_pairs)
        }

    def _pace_query_request(self, target_start: Optional[datetime]) -> None:
        """One start-to-start limit across routes, rounds and fallback URLs."""
        self._safe_checkpoint()
        previous = getattr(self, "_last_query_request_at", None)
        if previous is not None:
            remaining = self._current_query_interval(target_start) - (time.monotonic() - previous)
            if remaining > 0:
                self._sleep(remaining)
        self._safe_checkpoint()
        self._last_query_request_at = time.monotonic()

    def _round_candidates(self, target_start: Optional[datetime]) -> Iterator[Dict[str, Any]]:
        cart = self.cfg.cart_items

        # None means this route failed this round. Empty means success with no
        # usable tickets. Both are cached, but never carried into another round.
        route_tickets: dict[tuple[str, str], Optional[List[Dict[str, Any]]]] = {}
        attempted: set[tuple[str, str, str, str, str]] = set()
        for index, item in enumerate(cart, 1):
            self._safe_checkpoint()
            pair = (item.from_station, item.to_station)
            from_code, to_code = self._query_routes[pair]
            item_context = {"cart_index": index, "cart_total": len(cart),
                            "from_station": pair[0], "to_station": pair[1],
                            "train_code": item.train_code, "seat_label": item.seat_type,
                            "train_date": self.cfg.train_date}
            emit_event(self.event_sink, "cart_item",
                       f"备选 {index}/{len(cart)}：{pair[0]} → {pair[1]} · {item.train_code or '不限车次'} · {item.seat_type}",
                       **item_context)
            if pair not in route_tickets:
                result = self.client.query_tickets_result(
                    from_code, to_code, before_request=lambda: self._pace_query_request(target_start))
                self._safe_checkpoint()
                if not result.success:
                    route_tickets[pair] = None
                    message = f"{pair[0]} → {pair[1]} 查询失败，本轮跳过，下一轮重试"
                    logging.warning("%s（%s）", message, result.error)
                    emit_event(self.event_sink, "query_failed", message, **item_context)
                else:
                    valid_tickets = []
                    for ticket in result.tickets:
                        if (ticket.get("from_station_telecode") != from_code
                                or ticket.get("to_station_telecode") != to_code):
                            continue
                        # Standard station names were resolved to these exact
                        # response codes. A missing/inconsistent display map
                        # must not turn submission names into raw telecodes.
                        ticket = dict(ticket)
                        ticket.update(from_station=pair[0], to_station=pair[1])
                        valid_tickets.append(ticket)
                    rejected = len(result.tickets) - len(valid_tickets)
                    if rejected:
                        logging.warning("%s → %s 响应中 %s 条上下车站不匹配，已跳过", *pair, rejected)
                        emit_event(self.event_sink, "query_route_mismatch",
                                   f"{pair[0]} → {pair[1]} 的部分响应站点不匹配，已跳过", rejected_count=rejected,
                                   **item_context)
                    route_tickets[pair] = valid_tickets
            tickets = route_tickets[pair]
            if tickets is None:
                continue
            matching = False
            for ticket in sorted(tickets, key=lambda row: row.get("station_train_code", "").upper()):
                train_code = ticket.get("station_train_code", "").upper()
                if item.train_scope == "specific" and train_code != item.train_code:
                    continue
                if not ticket.get("can_buy"):
                    continue
                candidate = self._candidate_for_seat(ticket, item.seat_type)
                if candidate is None:
                    continue
                matching = True
                key = (RailwayClient._query_boarding_date(ticket), from_code, to_code,
                       ticket.get("train_no") or train_code, item.seat_type)
                if key in attempted:
                    continue
                attempted.add(key)
                candidate.update(item_context)
                candidate["train_code"] = train_code
                yield candidate
            if not matching:
                emit_event(self.event_sink, "query_empty",
                           f"备选 {index} 查询成功，当前没有可尝试的{item.seat_type}", **item_context)

    @staticmethod
    def _candidate_context(candidate: Dict[str, Any]) -> Dict[str, Any]:
        ticket = candidate["ticket"]
        context = {"from_station": candidate.get("from_station", ticket.get("from_station", "")),
                   "to_station": candidate.get("to_station", ticket.get("to_station", "")),
                   "train_code": ticket["station_train_code"],
                   "train_date": RailwayClient._query_boarding_date(ticket)}
        for name in ("cart_index", "cart_total"):
            if name in candidate:
                context[name] = candidate[name]
        return context

    def _candidate_for_seat(self, ticket: Dict[str, Any], seat_label: str) -> Optional[Dict[str, Any]]:
        spec = SEAT_SPECS[seat_label]
        stock = ticket["seats"].get(spec.stock_key, "--")
        if not _stock_available(stock):
            return None
        if spec.stock_key in SHARED_BERTH_CODES:
            raw_codes = ticket.get("seat_types")
            codes = set(raw_codes.strip()) if isinstance(raw_codes, str) else set()
            matching = codes & SHARED_BERTH_CODES[spec.stock_key]
            if not codes or len(matching) > 1:
                train_code = ticket["station_train_code"].upper()
                warning_key = (ticket.get("train_no") or train_code, spec.stock_key)
                if warning_key not in self._ambiguous_berth_warnings:
                    self._ambiguous_berth_warnings.add(warning_key)
                    labels = "软卧／一等卧" if spec.stock_key == "rw" else "硬卧／二等卧"
                    logging.warning("%s 的%s余票无法区分实际席别，已跳过；请核对 12306 官方余票", train_code, labels)
                return None
            if spec.submit_code not in matching:
                return None
        return {"ticket": ticket, "seat_label": seat_label,
                "seat_type": _resolve_submit_seat_code(seat_label, ticket), "stock": stock}

    def _book_ticket(self, candidate: Dict[str, Any], prepared_passengers: Dict[str, PreparedPassengerSet]) -> bool:
        if not self.safe_to_restart:
            raise AppError("已有已提交或待核对的订单，不能重复下单；请到 12306 官方订单页核对")
        self._safe_checkpoint()
        ticket = candidate["ticket"]
        seat_type = candidate["seat_type"]
        passengers = prepared_passengers[seat_type]
        submit_start = time.perf_counter()
        found_perf = candidate.get("found_perf")
        if isinstance(found_perf, float):
            _perf_log(self.cfg, "发现候选到提交请求准备耗时 %.1fms", (submit_start - found_perf) * 1000)
        logging.info("开始提交订单: %s %s", ticket["station_train_code"], candidate["seat_label"])
        self._phase(
            "submitting",
            f"正在提交 {ticket['station_train_code']} {candidate['seat_label']}",
            train=ticket["station_train_code"],
            seat_label=candidate["seat_label"],
            **self._candidate_context(candidate),
        )
        ok, message = self.client.submit_order_request(ticket)
        _perf_log(self.cfg, "submitOrderRequest 耗时 %.1fms", _elapsed_ms(submit_start))
        if not ok:
            self._check_student_rejection(message)
            logging.warning("提交订单请求失败: %s", message)
            return False
        self._safe_checkpoint()
        try:
            step_start = time.perf_counter()
            token, ticket_info = self.client.init_dc()
            _perf_log(self.cfg, "initDc 耗时 %.1fms", _elapsed_ms(step_start))
        except ResponseFormatError:
            # A malformed initDc response leaves the server-side reservation
            # state unknown. Do not continue to another candidate or queue.
            raise
        except AppError as exc:
            logging.warning("初始化确认订单页失败: %s", exc)
            return False
        self._safe_checkpoint()
        step_start = time.perf_counter()
        check_result = self.client.check_order_info(passengers, token)
        _perf_log(self.cfg, "checkOrderInfo 耗时 %.1fms", _elapsed_ms(step_start))
        if not check_result.success:
            self._check_student_rejection(check_result.message)
            logging.warning("订单信息校验失败: %s", check_result.message)
            return False
        self._safe_checkpoint()
        preference_seat_type = "WZ" if candidate["seat_label"] == "无座" else seat_type
        preference_payload = build_order_preference_payload(
            self.cfg.seat_relation_preference,
            self.cfg.berth_preference,
            check_result.capabilities,
            preference_seat_type,
            len(passengers.passengers),
            str(ticket_info.get("dw_flag") or ""),
            quiet_carriage_preference=getattr(self.cfg, "quiet_carriage_preference", False),
            quiet_carriage_available=ticket_info.get("quiet_carriage_available") is True,
        )
        self._report_preference_payload(preference_payload)
        step_start = time.perf_counter()
        ok, queue_data = self.client.get_queue_count(ticket, ticket_info, seat_type, token)
        _perf_log(self.cfg, "getQueueCount 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            self._check_student_rejection(queue_data)
            logging.warning("获取排队信息失败: %s", queue_data)
            return False
        # ``ticket`` inside this response is a credential-like value used by
        # the next confirmation request. Never stringify the whole mapping.
        logging.info("排队信息已获取，当前队列人数: %s", queue_data.get("count", "--"))
        left_ticket = queue_data.get("ticket") or ticket_info.get("leftTicketStr") or ticket.get("left_ticket", "")
        # This is the final cooperative cancellation boundary before the
        # irreversible order-confirmation request is sent.
        self._safe_checkpoint()
        self._set_order_state("confirm_sent")
        try:
            return self._confirm_and_wait(candidate, passengers, ticket_info, left_ticket, token, preference_payload)
        except Exception:
            if self.order_state in {"confirm_sent", "queued"}:
                self._set_order_state("unknown")
            raise

    def _confirm_and_wait(
        self, candidate: Dict[str, Any], passengers: PreparedPassengerSet,
        ticket_info: Dict[str, Any], left_ticket: str, token: str,
        preference_payload: OrderPreferencePayload,
    ) -> bool:
        ticket = candidate["ticket"]
        step_start = time.perf_counter()
        ok, message = self.client.confirm_single_for_queue(
            passengers,
            ticket_info,
            left_ticket,
            token,
            preference_payload,
        )
        _perf_log(self.cfg, "confirmSingleForQueue 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            self._set_order_state("safe")
            self._check_student_rejection(message)
            logging.warning("确认排队失败: %s", message)
            return False
        self._set_order_state("queued")
        logging.info("已提交排队，等待出票结果...")
        self._phase("queueing", "订单已提交，正在等待出票", seat_label=candidate["seat_label"],
                    **self._candidate_context(candidate))
        for _ in range(self.cfg.order_wait_attempts):
            if self.cancel_token.is_cancelled:
                raise AppError("订单已提交排队，停止操作仅终止了状态查询；请立即到 12306 官方订单页核对")
            try:
                ok, result = self.client.query_order_wait_time(token)
            except Exception as exc:
                raise AppError(
                    "订单已提交排队，但出票状态查询失败；任务已停止以避免重复下单，请到 12306 官方订单页核对"
                ) from exc
            if not ok:
                message = str(result.get("msg", result))
                if is_student_ticket_rejection(message):
                    self._set_order_state("safe")
                self._check_student_rejection(message)
                if _is_terminal_order_failure(message):
                    self._set_order_state("safe")
                    logging.warning("出票失败，放弃当前候选票: %s", message)
                    return False
                logging.warning("查询出票结果失败: %s", message)
                self._wait_after_order_submission()
                continue
            order_id = result.get("orderId")
            wait_time = result.get("waitTime")
            message = result.get("msg")
            if order_id:
                self._set_order_state("success")
                logging.info("抢票成功，订单号: %s。请尽快到 12306 完成支付。", order_id)
                emit_event(
                    self.event_sink,
                    "order_success",
                    "出票成功，请尽快前往 12306 支付",
                    order_id=str(order_id),
                    train=ticket["station_train_code"],
                    seat_label=candidate["seat_label"],
                    **self._candidate_context(candidate),
                )
                return True
            if is_student_ticket_rejection(message):
                self._set_order_state("safe")
            self._check_student_rejection(message)
            if _is_terminal_order_failure(message):
                self._set_order_state("safe")
                logging.warning("出票失败，放弃当前候选票: %s", message or f"waitTime={wait_time}")
                return False
            if message:
                logging.info("出票状态: %s", message)
            elif wait_time is not None:
                logging.info("排队中，预计等待 %s 秒", wait_time)
            emit_event(
                self.event_sink,
                "order_wait",
                str(message or "正在排队"),
                wait_time=wait_time,
            )
            self._wait_after_order_submission()
        raise AppError("出票状态暂时无法确认，任务已停止以避免重复下单；请到 12306 官方订单页核对")

    @staticmethod
    def _check_student_rejection(message: Any) -> None:
        if is_student_ticket_rejection(message):
            raise AppError("12306 明确拒绝本次学生票资质或优惠次数；请核对并修改相应乘车人票种后重新开始，不会自动改买成人票")

    def _wait_after_order_submission(self) -> None:
        try:
            self.cancel_token.wait(self.cfg.order_wait_interval_seconds)
        except RunCancelled as exc:
            raise AppError(
                "订单已提交排队，停止操作仅终止了状态查询；请立即到 12306 官方订单页核对"
            ) from exc

    def _report_preference_payload(self, payload: OrderPreferencePayload) -> None:
        for warning in payload.warnings:
            logging.warning("位置偏好降级: %s", warning)
            emit_event(
                self.event_sink,
                "preference_fallback",
                warning,
                choose_seats="",
                seat_detail_type="000",
            )
        if payload.choose_seats or payload.seat_detail_type != "000" or payload.is_jy == "Y":
            detail = []
            if payload.choose_seats:
                detail.append(f"座位关系 {payload.choose_seats}")
            if payload.seat_detail_type != "000":
                detail.append(f"铺位数量 {payload.seat_detail_type}")
            if payload.is_jy == "Y":
                detail.append("静音车厢优先，具体分配以 12306 为准")
            message = "，".join(detail)
            logging.info("本次订单位置软偏好: %s", message)
            emit_event(
                self.event_sink,
                "preference_applied",
                message,
                choose_seats=payload.choose_seats,
                seat_detail_type=payload.seat_detail_type,
                is_jy=payload.is_jy,
            )
