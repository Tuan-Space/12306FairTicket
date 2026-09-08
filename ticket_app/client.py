import base64
import logging
import re
import time
import urllib.parse
from http.cookiejar import MozillaCookieJar
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


_INIT_DC_SAFE_STOP = "尚未进入确认排队，本次任务已安全停止"


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
        try:
            response = self.session.post(
                f"{BASE_URL}/otn/login/checkUser",
                data={"_json_att": ""},
                timeout=self.cfg.request_timeout_seconds,
            )
            payload = response.json()
            return bool(payload.get("data", {}).get("flag"))
        except Exception as exc:
            logging.debug("检查登录状态失败: %s", exc)
            return False

    def ensure_login(self) -> None:
        self.cancel_token.checkpoint()
        if self.check_session():
            logging.info("当前登录会话仍然有效")
            return
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
                return
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

    def query_tickets(self, from_code: str, to_code: str) -> List[Dict[str, Any]]:
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
                request_start = time.perf_counter()
                response = self.session.get(
                    f"{BASE_URL}/otn/leftTicket/{endpoint}",
                    params=params,
                    timeout=self.cfg.request_timeout_seconds,
                )
                self.cancel_token.checkpoint()
                request_ms = _elapsed_ms(request_start)
                payload = response.json()
                if not payload.get("status"):
                    last_error = _message_from_payload(payload)
                    continue
                data = payload.get("data") or {}
                results = data.get("result") or []
                station_map = data.get("map") or {}
                parse_start = time.perf_counter()
                tickets = self._parse_tickets(results, station_map)
                _perf_log(
                    self.cfg,
                    "查票接口 %s: 请求 %.1fms，解析 %.1fms，结果 %s 条",
                    endpoint,
                    request_ms,
                    _elapsed_ms(parse_start),
                    len(tickets),
                )
                return tickets
            except RunCancelled:
                raise
            except Exception as exc:
                last_error = str(exc)
                logging.debug("余票查询接口 %s 失败: %s", endpoint, exc)
        if last_error:
            logging.warning("余票查询失败: %s", last_error)
        return []

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
                    "date": train_date,
                    "from_station": station_map.get(item(6), item(6)),
                    "to_station": station_map.get(item(7), item(7)),
                    "location_code": item(15),
                    "left_ticket": item(12),
                    "seats": seats,
                }
            )
        return tickets

    def get_passengers(self) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/getPassengerDTOs",
            data={"_json_att": ""},
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        passengers = payload.get("data", {}).get("normal_passengers")
        if not passengers:
            raise AppError(f"未获取到常用乘车人: {_message_from_payload(payload)}")
        return passengers

    @staticmethod
    def _response_json_object(response: Any, stage: str) -> Dict[str, Any]:
        """Read an order endpoint JSON response without exposing its contents."""

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ResponseFormatError(f"{stage} 返回的 JSON 格式无效，任务已停止") from exc
        if not isinstance(payload, dict):
            raise ResponseFormatError(f"{stage} 返回的 JSON 顶层不是对象，任务已停止")
        return payload

    def submit_order_request(self, ticket: Dict[str, Any]) -> Tuple[bool, str]:
        data = {
            "secretStr": ticket["secret_str"],
            "train_date": ticket["date"],
            "back_train_date": ticket["date"],
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
        token_match = re.search(r"globalRepeatSubmitToken\s*=\s*'([^']+)'", html)
        if not token_match:
            raise ResponseFormatError(f"initDc 返回缺少 REPEAT_SUBMIT_TOKEN；{_INIT_DC_SAFE_STOP}")
        ticket_info_text = _extract_js_object(html, "ticketInfoForPassengerForm")
        if not ticket_info_text:
            raise ResponseFormatError(f"initDc 返回缺少 ticketInfoForPassengerForm；{_INIT_DC_SAFE_STOP}")
        try:
            ticket_info = _parse_js_object(ticket_info_text)
        except ValueError as exc:
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
        data = {
            "train_date": _format_queue_date(ticket["date"]),
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
        payload = response.json()
        if payload.get("status"):
            return True, payload.get("data", {})
        return False, _message_from_payload(payload)

    def confirm_single_for_queue(
        self,
        passengers: PreparedPassengerSet,
        ticket_info: Dict[str, Any],
        left_ticket: str,
        token: str,
        preference_payload: Optional[OrderPreferencePayload] = None,
    ) -> Tuple[bool, str]:
        if preference_payload is None:
            # Never send an unverified legacy string without the capabilities
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
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        response = self.session.post(
            f"{BASE_URL}/otn/confirmPassenger/confirmSingleForQueue",
            data=data,
            timeout=self.cfg.request_timeout_seconds,
        )
        payload = response.json()
        if payload.get("status") and payload.get("data", {}).get("submitStatus"):
            return True, "OK"
        return False, _message_from_payload(payload)

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
        payload = response.json()
        if payload.get("status"):
            return True, payload.get("data", {})
        return False, {"msg": _message_from_payload(payload)}
