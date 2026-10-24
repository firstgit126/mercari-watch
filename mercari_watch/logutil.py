"""日志配置：控制台 + 轮转文件。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(level: str, log_path: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError:
            pass

    logging.getLogger("urllib3").setLevel(logging.WARNING)
