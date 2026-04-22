import json
import logging
import re
import time
from typing import Dict

import requests

from .configuration import AppConfig, AppError, STATION_URL


class StationStore:
    def __init__(self, session: requests.Session, cfg: AppConfig) -> None:
        self.session = session
        self.cfg = cfg
        self.stations: Dict[str, str] = {}

    def load(self) -> None:
        cached = self._read_cache(ignore_age=False)
        if cached:
            self.stations = cached
            logging.info("已加载站点缓存，共 %s 个站点", len(self.stations))
            return
        try:
            logging.info("正在从 12306 获取站点编码...")
            response = self.session.get(STATION_URL, timeout=self.cfg.request_timeout_seconds)
            response.raise_for_status()
            self.stations = self._parse_station_js(response.text)
            self._write_cache(self.stations)
            logging.info("站点编码更新完成，共 %s 个站点", len(self.stations))
        except Exception as exc:
            stale = self._read_cache(ignore_age=True)
            if stale:
                self.stations = stale
                logging.warning("站点编码更新失败，使用过期缓存: %s", exc)
                return
            raise AppError(f"无法获取站点编码: {exc}") from exc

    def code(self, station_name: str) -> str:
        if not self.stations:
            self.load()
        code = self.stations.get(station_name)
        if not code:
            raise AppError(f"未找到车站: {station_name}，请检查配置中的站名是否为 12306 标准站名")
        return code

    def _read_cache(self, ignore_age: bool) -> Dict[str, str]:
        path = self.cfg.station_cache_file
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = float(payload.get("fetched_at", 0))
            if not ignore_age:
                max_age = max(self.cfg.station_cache_days, 1) * 86400
                if time.time() - fetched_at > max_age:
                    return {}
            stations = payload.get("stations", {})
            if isinstance(stations, dict) and stations:
                return {str(k): str(v) for k, v in stations.items()}
        except Exception as exc:
            logging.debug("读取站点缓存失败: %s", exc)
        return {}

    def _write_cache(self, stations: Dict[str, str]) -> None:
        self.cfg.station_cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": time.time(), "stations": stations}
        self.cfg.station_cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _parse_station_js(text: str) -> Dict[str, str]:
        match = re.search(r"station_names\s*=\s*'([^']+)'", text)
        if not match:
            raise AppError("站点 JS 格式无法识别")
        stations: Dict[str, str] = {}
        for block in match.group(1).split("@"):
            if not block:
                continue
            fields = block.split("|")
            if len(fields) >= 3:
                name = fields[1].strip()
                code = fields[2].strip()
                if name and code:
                    stations[name] = code
        if not stations:
            raise AppError("没有解析到任何站点")
        return stations
