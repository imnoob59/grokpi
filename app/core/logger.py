"""Konfigurasi logging"""

import sys
import logging
from pathlib import Path


def setup_logger():
    """Mengatur logging berdasarkan konfigurasi DEBUG"""
    # Import tertunda untuk menghindari dependensi circular
    from app.core.config import settings, ROOT_DIR

    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]

    # Simpan log ke file lokal dalam mode DEBUG
    if settings.DEBUG:
        log_file = ROOT_DIR / "log.txt"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        handlers.append(file_handler)

    # Konfigurasi root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
        force=True
    )

    # Konfigurasi log uvicorn menggunakan format yang sama
    for uvicorn_logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers = handlers.copy()
        uvicorn_logger.setLevel(log_level)

    # Logger proyek
    _logger = logging.getLogger("grok-imagine")
    _logger.setLevel(log_level)
    return _logger


# Konfigurasi log Uvicorn (diteruskan ke uvicorn.run)
def get_uvicorn_log_config():
    """Mendapatkan konfigurasi log uvicorn"""
    from app.core.config import settings

    log_level = "DEBUG" if settings.DEBUG else "INFO"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": log_level, "propagate": False},
        },
    }


logger = setup_logger()
