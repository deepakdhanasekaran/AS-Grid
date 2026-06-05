#!/usr/bin/env python3
"""
Health check script for the Docker container.

The check is mode-aware so single-bot and multi-bot deployments can both
report healthy without relying on log files that do not exist in the other mode.
"""

import logging
import os
from datetime import datetime, timedelta


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(BASE_DIR, "log")
PID_FILE = os.path.join(BASE_DIR, "grid_bot.pid")


def _is_recent(path, max_age_seconds):
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    age = datetime.now() - mtime
    return age <= timedelta(seconds=max_age_seconds)


def _check_file(path, label, max_age_seconds=None):
    if not os.path.exists(path):
        logger.error("%s 不存在: %s", label, path)
        return False

    if os.path.getsize(path) == 0:
        logger.error("%s 为空: %s", label, path)
        return False

    if max_age_seconds is not None and not _is_recent(path, max_age_seconds):
        logger.error("%s 超过 %s 秒未更新: %s", label, max_age_seconds, path)
        return False

    logger.info("%s 正常: %s", label, path)
    return True


def _check_process_status():
    if not os.path.exists(PID_FILE):
        logger.warning("PID 文件不存在，跳过进程检查: %s", PID_FILE)
        return True

    try:
        with open(PID_FILE, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
        logger.info("主进程运行正常，PID: %s", pid)
        return True
    except Exception as exc:
        logger.error("进程检查失败: %s", exc)
        return False


def _check_single_mode(exchange):
    if exchange == "binance":
        log_candidates = [
            os.path.join(LOG_DIR, "binance_single_bot.log"),
            os.path.join(LOG_DIR, "binance_multi_bot.log"),
        ]
    else:
        log_candidates = [os.path.join(LOG_DIR, "gate_bot.log")]

    log_ok = False
    for candidate in log_candidates:
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            log_ok = _check_file(candidate, "单币种日志", max_age_seconds=300)
            if log_ok:
                break

    if not log_ok:
        logger.error("未找到可用的单币种日志文件")
        return False

    return _check_process_status()


def _check_multi_mode():
    checks = [
        _check_file(os.path.join(LOG_DIR, "status_summary.log"), "状态汇总日志", max_age_seconds=90),
        _check_file(os.path.join(LOG_DIR, "multi_grid_BN.log"), "主日志文件", max_age_seconds=600),
        _check_process_status(),
    ]

    bot_logs = [
        os.path.join(LOG_DIR, filename)
        for filename in os.listdir(LOG_DIR)
        if filename.startswith("grid_BN_") and filename.endswith(".log")
    ] if os.path.exists(LOG_DIR) else []

    if not bot_logs:
        logger.error("未找到任何币种日志文件")
        checks.append(False)
    else:
        recent_bot_logs = [
            path for path in bot_logs
            if os.path.getsize(path) > 0 and _is_recent(path, 900)
        ]
        if not recent_bot_logs:
            logger.error("币种日志文件存在，但最近 15 分钟内没有更新")
            checks.append(False)
        else:
            logger.info("检测到最近更新的币种日志: %s", ", ".join(os.path.basename(path) for path in recent_bot_logs))
            checks.append(True)

    return all(checks)


def main():
    logger.info("开始健康检查...")

    mode = os.getenv("GRID_MODE", "single").strip().lower()
    exchange = os.getenv("EXCHANGE", "gate").strip().lower()
    logger.info("当前模式: %s, 交易所: %s", mode, exchange)

    os.makedirs(LOG_DIR, exist_ok=True)

    if mode == "multi":
        healthy = _check_multi_mode()
    else:
        healthy = _check_single_mode(exchange)

    if healthy:
        logger.info("系统状态: 健康")
        raise SystemExit(0)

    logger.error("系统状态: 异常")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
