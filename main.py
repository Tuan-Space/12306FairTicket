import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from ticket_app.configuration import AppError, DEFAULT_CONFIG_FILE, load_config
from ticket_app.runner import TicketRunner


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="12306 命令行抢票助手")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="配置脚本路径，默认使用同目录 config.py")
    parser.add_argument("--validate-config", action="store_true", help="只校验配置，不登录、不访问 12306")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config_path = Path(args.config).resolve()
    try:
        cfg = load_config(config_path)
        configure_logging(cfg.log_level)
        logging.info("已加载配置: %s", cfg.config_path)
        if args.validate_config:
            logging.info("配置校验通过")
            return 0
        return TicketRunner(cfg).run()
    except AppError as exc:
        configure_logging("INFO")
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.warning("用户中断任务")
        return 130
    except Exception as exc:
        logging.exception("程序异常退出: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
