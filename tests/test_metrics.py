from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from message import MessageEnvelope
from metrics import (
    MetricsRegistry,
    set_metrics_registry,
    start_metrics_http_server,
)
from transport import InMemoryRadio, InMemoryRadioBus, MeshtasticTransport


def test_metrics_registry_prometheus_output() -> None:
    registry = MetricsRegistry()
    registry.inc("bridge_requests_total", labels={"command": "ping"})
    registry.set_gauge("bridge_inflight", 2, labels={"side": "gateway"})
    registry.observe(
        "bridge_latency_seconds",
        0.2,
        labels={"command": "ping"},
        buckets=(0.1, 0.5, 1.0),
    )
    registry.observe(
        "bridge_latency_seconds",
        0.7,
        labels={"command": "ping"},
        buckets=(0.1, 0.5, 1.0),
    )

    output = registry.render_prometheus()

    assert 'bridge_requests_total{command="ping"} 1.0' in output
    assert 'bridge_inflight{side="gateway"} 2' in output
    # Histogram emits cumulative buckets plus +inf
    assert 'bridge_latency_seconds_bucket{command="ping",le="0.5"} 1.0' in output
    assert 'bridge_latency_seconds_bucket{command="ping",le="+inf"} 2' in output
    assert 'bridge_latency_seconds_count{command="ping"} 2' in output


def test_metrics_http_endpoints() -> None:
    registry = MetricsRegistry()
    registry.inc("http_test_total")
    readiness_state = {"ready": True}

    def readiness() -> bool:
        return readiness_state["ready"]

    def status() -> dict[str, object]:
        return {"healthy": True}

    server = start_metrics_http_server("127.0.0.1", 0, registry, readiness, status)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    try:
        assert urlopen(base + "/health").read().decode("utf-8").strip() == "ok"
        metrics_body = urlopen(base + "/metrics").read().decode("utf-8")
        assert "http_test_total" in metrics_body

        status_body = urlopen(base + "/status").read().decode("utf-8")
        parsed_status = json.loads(status_body)
        assert parsed_status.get("status") == "ok"
        assert parsed_status["metrics"]["counters"]["http_test_total"]

        readiness_state["ready"] = False
        with pytest.raises(HTTPError) as excinfo:
            urlopen(base + "/ready")
        assert excinfo.value.code == 503
    finally:
        server.shutdown()
        server.server_close()


def test_transport_metrics_capture() -> None:
    registry = MetricsRegistry()
    set_metrics_registry(registry)

    bus = InMemoryRadioBus()
    radio_a = InMemoryRadio("a", bus)
    radio_b = InMemoryRadio("b", bus)

    transport_a = MeshtasticTransport(radio_a, spool_path=None)
    transport_b = MeshtasticTransport(radio_b, spool_path=None)

    envelope = MessageEnvelope(id="msg-1", type="request", command="test_echo", data={"value": 1})
    transport_a.send_message(envelope, "b", chunk_delay=0.0)
    sender, message = transport_b.receive_message(timeout=1.0)

    assert sender == "a"
    assert message is not None

    snapshot = registry.snapshot()
    message_keys = snapshot["counters"]["transport_messages_total"].keys()

    assert any('"direction": "outbound"' in key for key in message_keys)
    assert any('"direction": "inbound"' in key for key in message_keys)

    # Restore default registry for other tests
    set_metrics_registry(MetricsRegistry())
