import json
import logging

from cadns.log import JsonFormatter


def record(message="hello", level=logging.INFO, **extra):
    rec = logging.LogRecord("cadns.worker", level, "svc.py", 42, message, None, None)
    rec.__dict__.update(extra)
    return rec


def test_json_line_has_the_standard_fields():
    entry = json.loads(JsonFormatter(service="worker").format(record()))

    assert entry["message"] == "hello"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "cadns.worker"
    assert entry["service"] == "worker"
    assert entry["time"].endswith("Z")


def test_extra_fields_are_included():
    entry = json.loads(JsonFormatter().format(record(domain="www.un.org", addresses=3)))

    assert (entry["domain"], entry["addresses"]) == ("www.un.org", 3)


def test_exceptions_are_captured():
    try:
        raise RuntimeError("upstream exploded")
    except RuntimeError:
        import sys

        rec = record("measuring failed", logging.ERROR)
        rec.exc_info = sys.exc_info()

    entry = json.loads(JsonFormatter().format(rec))

    assert "RuntimeError: upstream exploded" in entry["exception"]


def test_message_arguments_are_formatted():
    rec = logging.LogRecord("cadns", logging.INFO, "f.py", 1, "queued %d domains", (5,), None)

    assert json.loads(JsonFormatter().format(rec))["message"] == "queued 5 domains"
