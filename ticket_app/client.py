import base64
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookiejar import MozillaCookieJar
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import requests

from .configuration import AppConfig, AppError, BASE_URL, PreparedPassengerSet, ResponseFormatError
from .configuration import _elapsed_ms, _perf_log
from .helpers import (
    _display_stock,
    _extract_js_object,
    _format_queue_date,
    _message_from_payload,
    _parse_js_object,
)
from .preferences import (
    OrderCapabilities,
    OrderCheckResult,
    OrderPreferencePayload,
)
from .runtime import CancellationToken, EventSink, RunCancelled, emit_event
from .quiet_capability import extract_quiet_carriage_available


_INIT_DC_SAFE_STOP = "尚未进入确认排队，本次任务已安全停止"


@dataclass(frozen=True)
class TicketQueryResult:
    """A successful empty response is different from an unavailable query."""

    success: bool
    tickets: List[Dict[str, Any]]
    error: str = ""


class RailwayClient:
    def __init__(
        self,
        cfg: AppConfig,
        event_sink: EventSink = None,
        cancel_token: Optional[CancellationToken] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.cfg = cfg
        self.event_sink = event_sink
        self.cancel_token = cancel_token or CancellationToken()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": f"{BASE_URL}/otn/leftTicket/init?linktypeid=dc",
                "Origin": BASE_URL,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        self.load_cookies()

    def load_cookies(self) -> None:
        if not self.cfg.persist_session:
            return
        path = self.cfg.session_file
        if not path.exists():
            return
        try:
            jar = MozillaCookieJar(str(path))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies.update(jar)
            logging.info("已加载登录会话缓存: %s", path)
        except Exception as exc:
            logging.warning("读取登录会话缓存失败，将重新登录: %s", exc)

    def save_cookies(self) -> None:
        if not self.cfg.persist_session:
            return
        path = self.cfg.session_file
        path.parent.mkdir(parents=True, exist_ok=True)
        jar = MozillaCookieJar(str(path))
        for cookie in self.session.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=True)

    def check_session(self) -> bool:
        """Distinguish a confirmed expiry from a failed/ambiguous check."""

        self.cancel_token.checkpoint()
        try:
            response = self.session.post(
                f"{BASE_URL}/otn/login/checkUser",
                data={"_json_att": ""},
                timeout=self.cfg.request_timeout_seconds,
            )
            self.cancel_token.checkpoint()
            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and not 200 <= status < 300:
                raise ValueError("HTTP error")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status", True) is not True:
                raise ValueError("invalid status")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("flag"), bool):
                raise ValueError("missing boolean flag")
            valid = data["flag"]
        except RunCancelled:
            raise
        except Exception as exc:
            self.cancel_token.checkpoint()
            if "response" in locals():
                self._log_response_diagnostics(response, "checkUser")
            logging.warning("登录状态检查失败（%s），未判定会话失效", type(exc).__name__)
            emit_event(self.event_sink, "session_checked", "检查失败，无法确认登录状态；请稍后重试",
                       state="failed", checked_at=time.time())
            raise AppError("检查登录状态失败，无法确认会话是否有效；请稍后重试") from None
        emit_event(self.event_sink, "session_checked",
                   "检查时已登录" if valid else "未登录或登录已失效",
                   state="valid" if valid else "expired", checked_at=time.time())
        return valid

    def ensure_login(self, *, check_first: bool = True) -> bool:
        """Return whether a fresh QR login occurred, optionally skipping a known-expired check."""

        self.cancel_token.checkpoint()
        session_valid = self.check_session() if check_first else False
        self.cancel_token.checkpoint()
        if session_valid:
            logging.info("当前登录会话仍然有效")
            emit_event(
                self.event_sink,
                "qr_status",
                "当前登录会话仍然有效，无需重新扫码",
                status="logged_in",
            )
            return False
        logging.info("需要扫码登录 12306")
        self._prefetch_login_cookies()
        image_bytes, uuid = self._create_qr_code()
        if self.cfg.persist_session:
            self.cfg.qr_code_file.parent.mkdir(parents=True, exist_ok=True)
            self.cfg.qr_code_file.write_bytes(image_bytes)
            logging.info("二维码已保存到: %s", self.cfg.qr_code_file)
        logging.info("请使用 12306 APP 扫码并确认登录")
        deadline = time.time() + self.cfg.login_qr_timeout_seconds
        emit_event(
            self.event_sink,
            "qr_ready",
            "请使用 12306 APP 扫码并确认登录",
            image_bytes=image_bytes,
            uuid=uuid,
            expires_at=deadline,
        )
        emit_event(self.event_sink, "qr_status", "等待扫码", status="waiting", uuid=uuid)
        scanned = False
        while time.time() < deadline:
            self.cancel_token.checkpoint()
            code, message = self._check_qr_status(uuid)
            if code == "0":
                pass
            elif code == "1":
                if not scanned:
                    logging.info("已扫码，等待手机端确认...")
                    emit_event(
                        self.event_sink,
                        "qr_status",
                        "已扫码，等待手机端确认",
                        status="scanned",
                        uuid=uuid,
                    )
                    scanned = True
            elif code == "2":
                ok, login_message = self._complete_login()
                if not ok:
                    raise AppError(f"扫码成功但登录校验失败: {login_message}")
                self.save_cookies()
                if self.cfg.persist_session:
                    logging.info("登录成功，会话已保存")
                else:
                    logging.info("登录成功（会话仅驻留内存）")
                emit_event(
                    self.event_sink,
                    "qr_status",
                    "登录成功",
                    status="confirmed",
                    uuid=uuid,
                )
                return True
            elif code == "3":
                emit_event(
                    self.event_sink,
                    "qr_status",
                    "二维码已过期",
                    status="expired",
                    uuid=uuid,
                )
                raise AppError("二维码已过期，请刷新二维码后重试")
            else:
                logging.warning("二维码状态异常: %s %s", code, message)
            self.cancel_token.wait(self.cfg.login_qr_poll_seconds)
        emit_event(
            self.event_sink,
            "qr_status",
            "二维码已过期",
            status="expired",
            uuid=uuid,
        )
        raise AppError("等待扫码登录超时，请刷新二维码后重试")

    def _prefetch_login_cookies(self) -> None:
        for url in (
            f"{BASE_URL}/otn/login/conf",
            f"{BASE_URL}/otn/index12306/getLoginBanner",
            f"{BASE_URL}/passport/web/auth/uamtk-static",
        ):
            self.cancel_token.checkpoint()
            try:
                self.session.get(url, timeout=self.cfg.request_timeout_seconds)
            except Exception:
                pass
            self.cancel_token.checkpoint()

    def _create_qr_code(self) -> Tuple[bytes, str]:
        response = self.session.post(
            f"{BASE_URL}/passport/web/create-qr64",
            data={"appid": "otn"},
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        if str(payload.get("result_code")) != "0":
            raise AppError(f"获取二维码失败: {payload.get('result_message') or payload}")
        image = payload.get("image")
        uuid = payload.get("uuid")
        if not image or not uuid:
            raise AppError("12306 未返回二维码图片或 UUID")
        try:
            image_bytes = base64.b64decode(image, validate=True)
        except (ValueError, TypeError) as exc:
            raise AppError("12306 返回的二维码图片格式无效") from exc
        return image_bytes, str(uuid)

    def _check_qr_status(self, uuid: str) -> Tuple[str, str]:
        response = self.session.post(
            f"{BASE_URL}/passport/web/checkqr",
            data={"uuid": uuid, "appid": "otn"},
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        return str(payload.get("result_code", "")), str(payload.get("result_message", ""))

    def _complete_login(self) -> Tuple[bool, str]:
        self.cancel_token.checkpoint()
        response = self.session.post(
            f"{BASE_URL}/passport/web/auth/uamtk",
            data={"appid": "otn"},
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        if str(payload.get("result_code")) != "0":
            return False, str(payload.get("result_message") or payload)
        token = payload.get("newapptk")
        if not token:
            return False, "未获取到 newapptk"
        self.cancel_token.checkpoint()
        response = self.session.post(
            f"{BASE_URL}/otn/uamauthclient",
            data={"tk": token},
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        if str(payload.get("result_code")) == "0":
            return True, str(payload.get("username") or "Success")
        return False, str(payload.get("result_message") or payload)

    def query_tickets_result(
        self, from_code: str, to_code: str,
        before_request: Optional[Callable[[], None]] = None,
    ) -> TicketQueryResult:
        params = {
            "leftTicketDTO.train_date": self.cfg.train_date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": self.cfg.purpose_codes,
        }
        last_error = ""
        for endpoint in ("query", "queryA", "queryZ"):
            self.cancel_token.checkpoint()
            try:
                if before_request is not None:
                    before_request()
                self.cancel_token.checkpoint()
                request_start = time.perf_counter()
                response = self.session.get(
                    f"{BASE_URL}/otn/leftTicket/{endpoint}",
                    params=params,
                    timeout=self.cfg.request_timeout_seconds,
                )
                self.cancel_token.checkpoint()
                request_ms = _elapsed_ms(request_start)
                status = getattr(response, "status_code", 200)
                if isinstance(status, int) and not 200 <= status < 300:
                    raise ValueError(f"HTTP {status}")
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") is not True:
                    last_error = "服务端未确认查询成功"
                    continue
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                    raise ValueError("余票响应缺少有效结果列表")
                results = data["result"]
                station_map = data.get("map") or {}
                if not isinstance(station_map, dict) or not all(isinstance(row, str) for row in results):
                    raise ValueError("余票响应格式错误")
                parse_start = time.perf_counter()
                tickets = self._parse_tickets(results, station_map)
                for ticket in tickets:
                    # Query column 13 is the train's originating date, not
                    # necessarily the boarding date at an intermediate stop.
                    ticket["query_date"] = self.cfg.train_date
                _perf_log(
                    self.cfg,
                    "查票接口 %s: 请求 %.1fms，解析 %.1fms，结果 %s 条",
                    endpoint,
                    request_ms,
                    _elapsed_ms(parse_start),
                    len(tickets),
                )
                return TicketQueryResult(True, tickets)
            except RunCancelled:
                raise
            except Exception as exc:
                # Exception bodies can include an upstream response or URL.
                # Keep diagnostics useful without copying ticket credentials.
                last_error = type(exc).__name__
                logging.debug("余票查询接口 %s 失败: %s", endpoint, last_error)
        if last_error:
            logging.warning("余票查询失败: %s", last_error)
        return TicketQueryResult(False, [], last_error or "未取得有效余票响应")

    @staticmethod
    def _parse_tickets(results: List[str], station_map: Dict[str, str]) -> List[Dict[str, Any]]:
        tickets: List[Dict[str, Any]] = []
        for raw in results:
            parts = raw.split("|")

            def item(index: int) -> str:
                return parts[index] if index < len(parts) else ""

            train_date = item(13)
            if len(train_date) == 8:
                train_date = f"{train_date[:4]}-{train_date[4:6]}-{train_date[6:]}"
            seats = {
                "swz": _display_stock(item(32)),
                "tz": _display_stock(item(25)),
                "ydz": _display_stock(item(31)),
                "edz": _display_stock(item(30)),
                "gr": _display_stock(item(21)),
                "rw": _display_stock(item(23)),
                "rz": _display_stock(item(24)),
                "yw": _display_stock(item(28)),
                "yz": _display_stock(item(29)),
                "wz": _display_stock(item(26)),
            }
            tickets.append(
                {
                    "secret_str": urllib.parse.unquote(item(0)),
                    "button_text": item(1),
                    "train_no": item(2),
                    "station_train_code": item(3),
                    "start_station_telecode": item(4),
                    "end_station_telecode": item(5),
                    "from_station_telecode": item(6),
                    "to_station_telecode": item(7),
                    "start_time": item(8),
                    "arrive_time": item(9),
                    "duration": item(10),
                    "can_buy": item(11) == "Y" or item(1) == "预订",
                    "start_train_date": train_date,
                    "from_station": station_map.get(item(6), item(6)),
                    "to_station": station_map.get(item(7), item(7)),
                    "location_code": item(15),
                    "left_ticket": item(12),
                    "seat_types": item(35),
                    "seats": seats,
                }
            )
        return tickets

    def get_passengers(self) -> List[Dict[str, Any]]:
        self.cancel_token.checkpoint()
        try:
            response = self.session.post(
                f"{BASE_URL}/otn/confirmPassenger/getPassengerDTOs",
                data={"_json_att": ""},
                timeout=self.cfg.request_timeout_seconds,
            )
            self.cancel_token.checkpoint()
            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and not 200 <= status < 300:
                raise ValueError("HTTP error")
            payload = response.json()
        except RunCancelled:
            raise
        except Exception:
            self.cancel_token.checkpoint()
            if "response" in locals():
                self._log_response_diagnostics(response, "getPassengerDTOs")
            raise AppError("读取账号乘车人失败，接口响应无法确认；请检查登录状态后重试") from None
        if not isinstance(payload, dict) or payload.get("status", True) is not True:
            raise AppError("读取账号乘车人失败；请检查登录状态后重试")
        data = payload.get("data")
        passengers = data.get("normal_passengers") if isinstance(data, dict) else None
        if not isinstance(passengers, list):
            raise AppError("12306 返回的乘车人列表格式无效，请稍后重试")
        if any(not isinstance(item, dict) or not isinstance(item.get("passenger_name"), str)
               or not item["passenger_name"].strip() for item in passengers):
            raise AppError("12306 返回的乘车人资料缺少有效姓名，请到官方渠道核对后重试")
        self.cancel_token.checkpoint()
        return [item.copy() for item in passengers]

    @staticmethod
    def _log_response_diagnostics(response: Any, stage: str) -> None:
        """Log shape/transport metadata only, never bodies, credentials or URL queries."""

        status = getattr(response, "status_code", None)
        status = status if isinstance(status, int) else None
        headers = getattr(response, "headers", {})
        raw_type = headers.get("Content-Type", "") if isinstance(headers, Mapping) else ""
        content_type = str(raw_type).split(";", 1)[0].strip().lower()
        if not re.fullmatch(r"[a-z0-9.+/-]{0,80}", content_type):
            content_type = "unknown"
        raw_url = getattr(response, "url", "")
        try:
            path = urllib.parse.urlsplit(raw_url).path if isinstance(raw_url, str) else ""
        except ValueError:
            path = ""
        # Only fixed endpoint labels are safe to display: redirect paths can
        # themselves contain credentials or passenger identifiers.
        endpoint = "other"
        for known in ("login", "checkUser", "getPassengerDTOs", "submitOrderRequest", "initDc", "checkOrderInfo",
                      "getQueueCount", "confirmSingleForQueue", "queryOrderWaitTime"):
            if known in path.split("/") or (known == "login" and "/login/" in path):
                endpoint = known
                break
        history = getattr(response, "history", ())
        redirects = len(history) if isinstance(history, (tuple, list)) else 0
        content = getattr(response, "content", None)
        length = len(content) if isinstance(content, bytes) else None
        prefix = content.lstrip()[:32].lower() if isinstance(content, bytes) else b""
        kind = "empty" if length == 0 else "html" if prefix.startswith((b"<!doctype html", b"<html")) else "other"
        logging.warning("%s 响应诊断: HTTP=%s, type=%s, endpoint=%s, redirects=%s, bytes=%s, body_kind=%s",
                        stage, status, content_type or "unknown", endpoint, redirects, length, kind)

    @staticmethod
    def _response_json_object(response: Any, stage: str) -> Dict[str, Any]:
        """Read an order endpoint JSON response without exposing its contents."""

        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and not 200 <= status < 300:
            RailwayClient._log_response_diagnostics(response, stage)
            raise ResponseFormatError(f"{stage} 返回 HTTP {status}，任务已停止；请核对官方订单状态")
        try:
            payload = response.json()
        except (TypeError, ValueError):
            RailwayClient._log_response_diagnostics(response, stage)
            raise ResponseFormatError(f"{stage} 返回的 JSON 格式无效，任务已停止") from None
        if not isinstance(payload, dict):
            RailwayClient._log_response_diagnostics(response, stage)
            raise ResponseFormatError(f"{stage} 返回的 JSON 顶层不是对象，任务已停止")
        return payload

    @staticmethod
    def _query_boarding_date(ticket: Dict[str, Any]) -> str:
        """Require the boarding date carried by the current query result."""
        boarding_date = ticket.get("query_date")
        if not isinstance(boarding_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", boarding_date):
            raise ResponseFormatError("本次余票缺少有效查询乘车日期，任务已安全停止，请重新查询")
        try:
            datetime.strptime(boarding_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ResponseFormatError("本次余票查询乘车日期无效，任务已安全停止，请重新查询") from exc
        return boarding_date

    def submit_order_request(self, ticket: Dict[str, Any]) -> Tuple[bool, str]:
        boarding_date = self._query_boarding_date(ticket)
        data = {
            "secretStr": ticket["secret_str"],
            "train_date": boarding_date,
            "back_train_date": boarding_date,
            "tour_flag": "dc",
            "purpose_codes": self.cfg.purpose_codes,
            "query_from_station_name": ticket["from_station"],
            "query_to_station_name": ticket["to_station"],
            "undefined": "",
        }
        response = self.session.post(
            f"{BASE_URL}/otn/leftTicket/submitOrderRequest",
            data=data,
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = self._response_json_object(response, "submitOrderRequest")
        if not isinstance(payload.get("status"), bool):
            raise ResponseFormatError("submitOrderRequest 返回缺少 status 布尔字段，任务已停止")
        if payload["status"]:
            return True, "OK"
        return False, _message_from_payload(payload)

    def init_dc(self) -> Tuple[str, Dict[str, Any]]:
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/initDc",
            data={"_json_att": ""},
            timeout=self.cfg.request_timeout_seconds,
        )
        html = response.text
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and not 200 <= status < 300:
            self._log_response_diagnostics(response, "initDc")
            raise ResponseFormatError(f"initDc 返回 HTTP {status}；{_INIT_DC_SAFE_STOP}")
        token_match = re.search(r"globalRepeatSubmitToken\s*=\s*'([^']+)'", html)
        if not token_match:
            self._log_response_diagnostics(response, "initDc")
            raise ResponseFormatError(f"initDc 返回缺少 REPEAT_SUBMIT_TOKEN；{_INIT_DC_SAFE_STOP}")
        ticket_info_text = _extract_js_object(html, "ticketInfoForPassengerForm")
        if not ticket_info_text:
            self._log_response_diagnostics(response, "initDc")
            raise ResponseFormatError(f"initDc 返回缺少 ticketInfoForPassengerForm；{_INIT_DC_SAFE_STOP}")
        try:
            ticket_info = _parse_js_object(ticket_info_text)
        except ValueError as exc:
            self._log_response_diagnostics(response, "initDc")
            raise ResponseFormatError(
                f"initDc 返回的 ticketInfoForPassengerForm 格式无效；{_INIT_DC_SAFE_STOP}"
            ) from exc
        order_request = ticket_info.get("orderRequestDTO")
        if order_request is not None and not isinstance(order_request, dict):
            raise ResponseFormatError(f"initDc 返回的 orderRequestDTO 结构无效；{_INIT_DC_SAFE_STOP}")
        query_request = ticket_info.get("queryLeftTicketRequestDTO")
        # ``getQueueCount`` consumes this object after checkOrderInfo.  A
        # truthy non-mapping would otherwise become a late AttributeError,
        # which both loses the safe-stop explanation and obscures the initDc
        # protocol failure.
        if query_request is not None and not isinstance(query_request, Mapping):
            raise ResponseFormatError(
                f"initDc 返回的 queryLeftTicketRequestDTO 结构无效；{_INIT_DC_SAFE_STOP}"
            )
        if isinstance(order_request, dict):
            dw_flag = order_request.get("dw_flag")
            if isinstance(dw_flag, str):
                # The current official seat UI reads this exact nested field.
                # Copying it to the normalized context keeps protocol code out
                # of the GUI/runner while retaining the unmodified DTO.
                ticket_info["dw_flag"] = dw_flag
        ticket_info["quiet_carriage_available"] = extract_quiet_carriage_available(html)
        return token_match.group(1), ticket_info

    def check_order_info(self, passengers: PreparedPassengerSet, token: str) -> OrderCheckResult:
        data = {
            "cancel_flag": "2",
            "bed_level_order_num": "000000000000000000000000000000",
            "passengerTicketStr": passengers.passenger_ticket_str,
            "oldPassengerStr": passengers.old_passenger_str,
            "tour_flag": "dc",
            "randCode": "",
            "whatsSelect": "1",
            "sessionId": "",
            "sig": "",
            "scene": "nc_login",
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/checkOrderInfo",
            data=data,
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = self._response_json_object(response, "checkOrderInfo")
        if not isinstance(payload.get("status"), bool):
            raise ResponseFormatError("checkOrderInfo 返回缺少 status 布尔字段，任务已停止")
        response_data = payload.get("data")
        if payload["status"] and not isinstance(response_data, dict):
            raise ResponseFormatError("checkOrderInfo 返回的 data 结构无效，任务已停止")
        response_data = response_data if isinstance(response_data, dict) else {}
        if payload["status"] and not isinstance(response_data.get("submitStatus"), bool):
            # This endpoint is reached only after submitOrderRequest.  Do not
            # treat an incomplete or type-shifted success response as an
            # ordinary rejection and then try another candidate.
            raise ResponseFormatError(
                "checkOrderInfo 返回缺少或包含无效的 submitStatus 布尔字段，任务已停止"
            )
        capabilities = OrderCapabilities.from_mapping(response_data)
        success = payload["status"] is True and response_data.get("submitStatus") is True
        message = "OK" if success else _message_from_payload(payload)
        return OrderCheckResult(success, message, capabilities)

    def get_queue_count(self, ticket: Dict[str, Any], ticket_info: Dict[str, Any], seat_type: str, token: str) -> Tuple[bool, Any]:
        query_dto = ticket_info.get("queryLeftTicketRequestDTO") or {}
        # A stale confirmation page must not redirect a cart candidate to a
        # different train or boarding segment. Optional missing DTO fields still
        # fall back to this round's query result below.
        for key, label in (("train_no", "车次编号"), ("station_train_code", "车次"),
                           ("from_station_telecode", "出发站"), ("to_station_telecode", "到达站")):
            actual, expected = query_dto.get(key), ticket.get(key)
            if actual not in (None, "") and expected not in (None, "") and actual != expected:
                raise ResponseFormatError(
                    f"订单页{label}与本次候选不一致，任务已安全停止；请核对 12306 订单"
                )
        data = {
            "train_date": _format_queue_date(self._queue_boarding_date(ticket, ticket_info)),
            "train_no": query_dto.get("train_no") or ticket.get("train_no", ""),
            "stationTrainCode": query_dto.get("station_train_code") or ticket.get("station_train_code", ""),
            "seatType": seat_type,
            "fromStationTelecode": query_dto.get("from_station_telecode") or ticket.get("from_station_telecode", ""),
            "toStationTelecode": query_dto.get("to_station_telecode") or ticket.get("to_station_telecode", ""),
            "leftTicket": ticket_info.get("leftTicketStr") or ticket.get("left_ticket", ""),
            "purpose_codes": "00",
            "train_location": ticket_info.get("train_location") or ticket.get("location_code", ""),
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/getQueueCount",
            data=data,
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = self._response_json_object(response, "getQueueCount")
        if payload.get("status"):
            return True, payload.get("data", {})
        return False, _message_from_payload(payload)

    @staticmethod
    def _queue_boarding_date(ticket: Dict[str, Any], ticket_info: Dict[str, Any]) -> str:
        """Use the server's order date, checked against this query's date.

        The official confirmation page reads orderRequestDTO.train_date.time;
        start_train_date from the query row is a different field. Contexts
        without an order-date DTO retain the already submitted boarding date.
        """
        boarding_date = RailwayClient._query_boarding_date(ticket)
        order = ticket_info.get("orderRequestDTO") or {}
        if "train_date" not in order:
            return boarding_date
        raw = order["train_date"]
        timestamp = raw.get("time") if isinstance(raw, Mapping) else None
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ResponseFormatError("订单页乘车日期格式无效，任务已安全停止；请核对 12306 订单")
        try:
            server_date = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError) as exc:
            raise ResponseFormatError("订单页乘车日期格式无效，任务已安全停止；请核对 12306 订单") from exc
        if server_date != boarding_date:
            raise ResponseFormatError("订单页乘车日期与本次查询不一致，任务已安全停止；请核对 12306 订单")
        return server_date

    def confirm_single_for_queue(
        self,
        passengers: PreparedPassengerSet,
        ticket_info: Dict[str, Any],
        left_ticket: str,
        token: str,
        preference_payload: Optional[OrderPreferencePayload] = None,
    ) -> Tuple[bool, str]:
        if preference_payload is None:
            # Never send seat preferences without the capabilities
            # returned by checkOrderInfo. The runner passes an explicit payload.
            preference_payload = OrderPreferencePayload()
        data = {
            "passengerTicketStr": passengers.passenger_ticket_str,
            "oldPassengerStr": passengers.old_passenger_str,
            "randCode": "",
            "purpose_codes": "00",
            "key_check_isChange": ticket_info.get("key_check_isChange", ""),
            "leftTicketStr": left_ticket,
            "train_location": ticket_info.get("train_location", ""),
            "choose_seats": preference_payload.choose_seats,
            "seatDetailType": preference_payload.seat_detail_type,
            "whatsSelect": "1",
            "roomType": "00",
            "dwAll": "N",
            "is_jy": preference_payload.is_jy,
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/confirmSingleForQueue",
            data=data,
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = self._response_json_object(response, "confirmSingleForQueue")
        # This request may already have created a server-side order. Only a
        # literal boolean refusal permits the runner to try another candidate;
        # a missing field or a truthy string cannot establish that it is safe.
        if not isinstance(payload.get("status"), bool):
            raise ResponseFormatError(
                "confirmSingleForQueue 返回缺少或包含无效的 status 布尔字段，订单结果无法确认；请核对官方订单状态"
            )
        if payload["status"] is False:
            return False, _message_from_payload(payload)
        response_data = payload.get("data")
        if not isinstance(response_data, dict):
            raise ResponseFormatError(
                "confirmSingleForQueue 返回的 data 结构无效，订单结果无法确认；请核对官方订单状态"
            )
        if not isinstance(response_data.get("submitStatus"), bool):
            raise ResponseFormatError(
                "confirmSingleForQueue 返回缺少或包含无效的 submitStatus 布尔字段，订单结果无法确认；请核对官方订单状态"
            )
        if response_data["submitStatus"] is False:
            return False, _message_from_payload(payload)
        return True, "OK"

    def query_order_wait_time(self, token: str) -> Tuple[bool, Dict[str, Any]]:
        response = self.session.get(
            f"{BASE_URL}/otn/confirmPassenger/queryOrderWaitTime",
            params={
                "random": int(time.time() * 1000),
                "tourFlag": "dc",
                "_json_att": "",
                "REPEAT_SUBMIT_TOKEN": token,
            },
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = self._response_json_object(response, "queryOrderWaitTime")
        if not isinstance(payload.get("status"), bool):
            raise ResponseFormatError(
                "queryOrderWaitTime 返回缺少或包含无效的 status 布尔字段，订单结果无法确认；请核对官方订单状态"
            )
        response_data = payload.get("data")
        if payload["status"] is True:
            if not isinstance(response_data, dict):
                raise ResponseFormatError(
                    "queryOrderWaitTime 返回的 data 结构无效，订单结果无法确认；请核对官方订单状态"
                )
            for key in ("orderId", "msg"):
                value = response_data.get(key)
                if value is not None and not isinstance(value, str):
                    raise ResponseFormatError(
                        f"queryOrderWaitTime 返回的 {key} 类型无效，订单结果无法确认；请核对官方订单状态"
                    )
            wait_time = response_data.get("waitTime")
            if wait_time is not None and (isinstance(wait_time, bool) or not isinstance(wait_time, int)):
                raise ResponseFormatError(
                    "queryOrderWaitTime 返回的 waitTime 类型无效，订单结果无法确认；请核对官方订单状态"
                )
            return True, response_data

        # Only actual textual error fields may establish a terminal refusal.
        # Stringifying an arbitrary response/collection can make an unrelated
        # nested '出票失败' value look like a definite order rejection.
        messages = payload.get("messages")
        if messages is not None and not isinstance(messages, str):
            if not isinstance(messages, list) or any(not isinstance(item, str) for item in messages):
                raise ResponseFormatError(
                    "queryOrderWaitTime 返回的 messages 类型无效，订单结果无法确认；请核对官方订单状态"
                )
        message_values = []
        if isinstance(response_data, dict):
            message_values.extend(response_data.get(key) for key in ("errMsg", "msg", "message"))
        message_values.extend(payload.get(key) for key in ("message", "result_message"))
        if any(value is not None and not isinstance(value, str) for value in message_values):
            raise ResponseFormatError(
                "queryOrderWaitTime 返回的错误消息类型无效，订单结果无法确认；请核对官方订单状态"
            )
        primary_message = messages[0] if isinstance(messages, list) and messages else messages
        message = next((value for value in (primary_message, *message_values) if isinstance(value, str) and value), "")
        return False, {"msg": message or "出票状态查询未返回明确结果"}
