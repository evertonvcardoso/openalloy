# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Log formatting (from Linuity's logging_config, kept nearly verbatim)."""

import logging
import sys

_LEVEL_PREFIX = {
    logging.DEBUG: "DBG",
    logging.INFO: " + ",
    logging.WARNING: " ! ",
    logging.ERROR: " x ",
    logging.CRITICAL: "!!!",
}


class OpenAlloyFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.short_name = record.name.split(".")[-1]
        record.level_prefix = _LEVEL_PREFIX.get(record.levelno, "   ")
        return super().format(record)


class _CurrentStream:
    """Writes to whatever sys.stdout/sys.stderr is *now*: a handler bound to the
    stream object of the moment breaks when that object is replaced and closed
    (embedding, test runners)."""

    def __init__(self, name: str):
        self.name = name

    def write(self, text: str) -> int:
        return getattr(sys, self.name).write(text)

    def flush(self) -> None:
        stream = getattr(sys, self.name)
        if not getattr(stream, "closed", False):
            stream.flush()


_base_factory = logging.getLogRecordFactory()


def _english_record(*args, **kwargs) -> logging.LogRecord:
    """Every log record gets its translatable parts (i18n.Message, exceptions that carry
    one) in English, whatever the interface language: the journal stays in one
    language (docs/DEVELOPMENT.md) while the window and the CLI show the same errors
    translated."""
    from openalloy.i18n import Message, english
    record = _base_factory(*args, **kwargs)

    def conv(value):
        return english(value) if isinstance(value, (Message, BaseException)) else value
    if isinstance(record.msg, Message):
        record.msg = record.msg.english()
    if isinstance(record.args, tuple):
        record.args = tuple(conv(a) for a in record.args)
    elif isinstance(record.args, dict):
        record.args = {k: conv(v) for k, v in record.args.items()}
    return record


def install_english_records() -> None:
    global _base_factory
    current = logging.getLogRecordFactory()
    if current is not _english_record:
        _base_factory = current
        logging.setLogRecordFactory(_english_record)


def setup_logging(daemon: bool = False, verbose: bool = False) -> None:
    install_english_records()
    if daemon:
        fmt = "[%(level_prefix)s]  %(short_name)-12s %(message)s"
    else:
        fmt = "[%(level_prefix)s]  %(message)s"
    handler = logging.StreamHandler(_CurrentStream("stdout" if daemon else "stderr"))
    handler.setFormatter(OpenAlloyFormatter(fmt=fmt))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO if daemon else logging.WARNING)
