import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from .client import RailwayClient
from .clock import ServerClock
from .configuration import AppConfig, AppError, PreparedPassengerSet, ResponseFormatError, SEAT_SPECS, _elapsed_ms, _perf_log, _resolve_schedule_time
from .helpers import _build_passenger_strings, _is_terminal_order_failure, _resolve_submit_seat_code, _stock_available
from .preferences import OrderPreferencePayload, build_order_preference_payload
from .runtime import CancellationToken, EventSink, RunCancelled, emit_event
from .stations import StationStore


class TicketRunner:
    def __init__(
        self,
        cfg: AppConfig,
        event_sink: EventSink = None,
        cancel_token: CancellationToken | None = None,
        session: Optional[requests.Session] = None,
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
        self.clock = ServerClock(self.client.session, cfg)
        self.stations = StationStore(self.client.session, cfg)
        self.preferred_order = {train: index for index, train in enumerate(cfg.preferred_trains)}
        self.seat_sequence = [(seat_label, SEAT_SPECS[seat_label]) for seat_label in cfg.seat_types]

    def run(self) -> int:
        try:
            result = self._run()
            emit_event(self.event_sink, "finished", "任务已完成", exit_code=result)
            return result
        except RunCancelled:
            logging.warning("用户已停止任务")
            emit_event(self.event_sink, "phase", "任务已停止", phase="cancelled")
            emit_event(self.event_sink, "finished", "任务已停止", exit_code=130)
            return 130
        except Exception as exc:
            emit_event(self.event_sink, "error", str(exc), exception_type=type(exc).__name__)
            raise

    def _run(self) -> int:
        self._phase("clock", "正在同步 12306 服务器时间")
        self.clock.sync(self.cancel_token, self.event_sink)
        self._phase("stations", "正在加载车站信息")
        self.stations.load(self.cancel_token, self.event_sink)
        from_code = self.stations.code(self.cfg.from_station)
        to_code = self.stations.code(self.cfg.to_station)
        self._phase("login", "正在检查登录状态")
        self.client.ensure_login()
        self.cancel_token.checkpoint()
        selected_passengers = self._select_passengers() if self.cfg.auto_submit else []
        prepared_passengers = self._prepare_passengers_by_seat_code(selected_passengers) if self.cfg.auto_submit else {}
        target_start = self._resolve_target_start()
        self._wait_for_query_start(target_start)
        self._phase("querying", "开始查询余票")
        logging.info(
            "任务启动: %s -> %s, 日期 %s, 座席 %s",
            self.cfg.from_station,
            self.cfg.to_station,
            self.cfg.train_date,
            ",".join(self.cfg.seat_types),
        )
        first_query_perf = time.perf_counter()
        for attempt in range(1, self.cfg.max_retries + 1):
            self.cancel_token.checkpoint()
            if self._should_stop():
                logging.info("已到 STOP_AT，任务结束")
                emit_event(self.event_sink, "phase", "已到停止时间", phase="stopped")
                return 1
            logging.info("第 %s/%s 次查询余票...", attempt, self.cfg.max_retries)
            emit_event(
                self.event_sink,
                "query",
                f"第 {attempt}/{self.cfg.max_retries} 次查询",
                attempt=attempt,
                max_retries=self.cfg.max_retries,
                interval_seconds=self._current_query_interval(target_start),
            )
            query_start = time.perf_counter()
            if attempt == 1:
                _perf_log(self.cfg, "等待结束到首个查票请求准备耗时 %.1fms", _elapsed_ms(first_query_perf))
            tickets = self.client.query_tickets(from_code, to_code)
            find_start = time.perf_counter()
            candidates = self._find_candidates(tickets)
            _perf_log(
                self.cfg,
                "本轮查票总耗时 %.1fms，筛选 %.1fms，候选 %s 个",
                _elapsed_ms(query_start),
                _elapsed_ms(find_start),
                len(candidates),
            )
            if not candidates:
                self._sleep(self._current_query_interval(target_start))
                continue
            for candidate in candidates:
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
                )
                if not self.cfg.auto_submit:
                    continue
                if self._book_ticket(candidate, prepared_passengers):
                    return 0
            self._sleep(self._current_query_interval(target_start))
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
        by_name = {item.get("passenger_name"): item for item in passengers}
        missing = [name for name in self.cfg.passenger_names if name not in by_name]
        if missing:
            raise AppError(f"配置中的乘车人不存在: {missing}。请在 12306 常用乘车人中核对姓名")
        selected = [by_name[name] for name in self.cfg.passenger_names]
        logging.info("已选择乘车人: %s", "、".join(self.cfg.passenger_names))
        emit_event(
            self.event_sink,
            "passengers",
            "已加载常用乘车人",
            available_count=len(by_name),
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
        if not target_start:
            return
        now = self.clock.now()
        if len(self.cfg.start_at) == 8 and target_start < now:
            logging.info("START_AT 已早于当前服务器时间，立即开始")
            return
        query_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        if query_start <= now:
            logging.info("已进入热身查询窗口，立即开始；目标开售时间 %s", target_start.strftime("%Y-%m-%d %H:%M:%S"))
            return
        logging.info(
            "等待热身查询窗口: %s；目标开售时间: %s",
            query_start.strftime("%Y-%m-%d %H:%M:%S"),
            target_start.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._phase(
            "waiting",
            "等待进入热身查询窗口",
            target_timestamp=target_start.timestamp(),
            query_start_timestamp=query_start.timestamp(),
        )
        self.clock.sleep_until(query_start, self.cancel_token)

    def _should_stop(self) -> bool:
        now = self.clock.now()
        stop_at = _resolve_schedule_time(self.cfg.stop_at, now)
        if not stop_at:
            return False
        if len(self.cfg.stop_at) == 8 and stop_at < now:
            stop_at += timedelta(days=1)
        return now >= stop_at

    def _current_query_interval(self, target_start: Optional[datetime]) -> float:
        if not target_start:
            return self.cfg.query_interval_seconds
        now = self.clock.now()
        hot_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        hot_end = target_start + timedelta(seconds=self.cfg.hot_window_seconds)
        if hot_start <= now <= hot_end:
            return self.cfg.hot_query_interval_seconds
        return self.cfg.query_interval_seconds

    def _find_candidates(self, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def sort_key(ticket: Dict[str, Any]) -> Tuple[int, int, str]:
            train = ticket["station_train_code"].upper()
            if not self.preferred_order:
                return (0, 0, train)
            if train in self.preferred_order:
                return (0, self.preferred_order[train], train)
            return (1, len(self.preferred_order), train)

        candidates: List[Dict[str, Any]] = []
        for ticket in sorted(tickets, key=sort_key):
            train_code = ticket["station_train_code"].upper()
            if self.cfg.preferred_trains and self.cfg.only_preferred_trains and train_code not in self.preferred_order:
                continue
            if not ticket["can_buy"]:
                continue
            for seat_label, spec in self.seat_sequence:
                stock = ticket["seats"].get(spec.stock_key, "--")
                if not _stock_available(stock):
                    continue
                candidates.append(
                    {
                        "ticket": ticket,
                        "seat_label": seat_label,
                        "seat_type": _resolve_submit_seat_code(seat_label, ticket),
                        "stock": stock,
                    }
                )
        return candidates

    def _book_ticket(self, candidate: Dict[str, Any], prepared_passengers: Dict[str, PreparedPassengerSet]) -> bool:
        self.cancel_token.checkpoint()
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
        )
        ok, message = self.client.submit_order_request(ticket)
        _perf_log(self.cfg, "submitOrderRequest 耗时 %.1fms", _elapsed_ms(submit_start))
        if not ok:
            logging.warning("提交订单请求失败: %s", message)
            return False
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
        step_start = time.perf_counter()
        check_result = self.client.check_order_info(passengers, token)
        _perf_log(self.cfg, "checkOrderInfo 耗时 %.1fms", _elapsed_ms(step_start))
        if not check_result.success:
            logging.warning("订单信息校验失败: %s", check_result.message)
            return False
        preference_seat_type = "" if candidate["seat_label"] == "无座" else seat_type
        preference_payload = build_order_preference_payload(
            self.cfg.seat_relation_preference,
            self.cfg.berth_preference,
            check_result.capabilities,
            preference_seat_type,
            len(passengers.passengers),
            str(ticket_info.get("dw_flag") or ""),
        )
        self._report_preference_payload(preference_payload)
        step_start = time.perf_counter()
        ok, queue_data = self.client.get_queue_count(ticket, ticket_info, seat_type, token)
        _perf_log(self.cfg, "getQueueCount 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            logging.warning("获取排队信息失败: %s", queue_data)
            return False
        # ``ticket`` inside this response is a credential-like value used by
        # the next confirmation request. Never stringify the whole mapping.
        logging.info("排队信息已获取，当前队列人数: %s", queue_data.get("count", "--"))
        left_ticket = queue_data.get("ticket") or ticket_info.get("leftTicketStr") or ticket.get("left_ticket", "")
        # This is the final cooperative cancellation boundary before the
        # irreversible order-confirmation request is sent.
        self.cancel_token.checkpoint()
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
            logging.warning("确认排队失败: %s", message)
            return False
        logging.info("已提交排队，等待出票结果...")
        self._phase("queueing", "订单已提交，正在等待出票")
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
                if _is_terminal_order_failure(message):
                    logging.warning("出票失败，放弃当前候选票: %s", message)
                    return False
                logging.warning("查询出票结果失败: %s", message)
                self._wait_after_order_submission()
                continue
            order_id = result.get("orderId")
            wait_time = result.get("waitTime")
            message = result.get("msg")
            if order_id:
                logging.info("抢票成功，订单号: %s。请尽快到 12306 完成支付。", order_id)
                emit_event(
                    self.event_sink,
                    "order_success",
                    "出票成功，请尽快前往 12306 支付",
                    order_id=str(order_id),
                    train=ticket["station_train_code"],
                    seat_label=candidate["seat_label"],
                )
                return True
            if _is_terminal_order_failure(message):
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
        if payload.choose_seats or payload.seat_detail_type != "000":
            detail = []
            if payload.choose_seats:
                detail.append(f"座位关系 {payload.choose_seats}")
            if payload.seat_detail_type != "000":
                detail.append(f"铺位数量 {payload.seat_detail_type}")
            message = "，".join(detail)
            logging.info("本次订单位置软偏好: %s", message)
            emit_event(
                self.event_sink,
                "preference_applied",
                message,
                choose_seats=payload.choose_seats,
                seat_detail_type=payload.seat_detail_type,
            )
