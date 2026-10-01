import json
import logging
import threading

import pytest

from gatekeeper.logs import event_id_var, setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _log_in_visit(event_id: str, msg: str) -> None:
    def worker():
        event_id_var.set(event_id)
        logging.getLogger("gatekeeper.test").info(msg)

    t = threading.Thread(target=worker)
    t.start()
    t.join()


def test_text_lines_carry_event_id(capsys):
    setup_logging("INFO", "text")
    _log_in_visit("evt-1", "hello")
    logging.getLogger("gatekeeper.test").info("outside")
    lines = capsys.readouterr().out.splitlines()
    assert "[evt-1] hello" in lines[0]
    assert "[-] outside" in lines[1]


def test_json_lines(capsys):
    setup_logging("debug", "json")
    _log_in_visit("evt-2", "transcript: 'hi'")
    record = json.loads(capsys.readouterr().out.splitlines()[0])
    assert record["event_id"] == "evt-2"
    assert record["msg"] == "transcript: 'hi'"
    assert record["level"] == "INFO"
    assert record["logger"] == "gatekeeper.test"


def test_json_includes_exceptions(capsys):
    setup_logging("INFO", "json")
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("x").exception("failed")
    record = json.loads(capsys.readouterr().out.splitlines()[0])
    assert "ValueError: bad" in record["exc"]
