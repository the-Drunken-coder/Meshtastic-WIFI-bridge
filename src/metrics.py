from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_LATENCY_BUCKETS: Tuple[float, ...] = (
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


def _labels_key(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    if not labels:
        return tuple()
    return tuple(sorted(labels.items()))


class CounterMetric:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._samples: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + amount

    def samples(self) -> Dict[Tuple[Tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._samples)


class GaugeMetric:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._samples: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._samples[key] = value

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        self.inc(-amount, labels=labels)

    def samples(self) -> Dict[Tuple[Tuple[str, str], ...], float]:
        with self._lock:
            return dict(self._samples)


class HistogramMetric:
    def __init__(
        self, name: str, buckets: Iterable[float] = DEFAULT_LATENCY_BUCKETS, description: str = ""
    ) -> None:
        self.name = name
        self.description = description
        self.buckets = tuple(sorted(buckets))
        self._counts: Dict[Tuple[Tuple[str, str], ...], List[float]] = {}
        self._sums: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._total_counts: Dict[Tuple[Tuple[str, str], ...], int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0.0 for _ in self.buckets])
            matched_index = None
            for idx, bound in enumerate(self.buckets):
                if value <= bound:
                    matched_index = idx
                    break
            if matched_index is None and counts:
                matched_index = len(counts) - 1

            if matched_index is not None:
                for idx in range(matched_index, len(counts)):
                    counts[idx] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._total_counts[key] = self._total_counts.get(key, 0) + 1

    def samples(self) -> Dict[Tuple[Tuple[str, str], ...], Dict[str, object]]:
        with self._lock:
            snapshot: Dict[Tuple[Tuple[str, str], ...], Dict[str, object]] = {}
            for key, counts in self._counts.items():
                snapshot[key] = {
                    "counts": list(counts),
                    "sum": self._sums.get(key, 0.0),
                    "count": self._total_counts.get(key, 0),
                }
            return snapshot


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: Dict[str, CounterMetric] = {}
        self._gauges: Dict[str, GaugeMetric] = {}
        self._histograms: Dict[str, HistogramMetric] = {}
        self._lock = threading.Lock()

    # Metric creation helpers -------------------------------------------------
    def counter(self, name: str, description: str = "") -> CounterMetric:
        with self._lock:
            metric = self._counters.get(name)
            if metric is None:
                metric = CounterMetric(name, description)
                self._counters[name] = metric
            return metric

    def gauge(self, name: str, description: str = "") -> GaugeMetric:
        with self._lock:
            metric = self._gauges.get(name)
            if metric is None:
                metric = GaugeMetric(name, description)
                self._gauges[name] = metric
            return metric

    def histogram(
        self,
        name: str,
        description: str = "",
        buckets: Iterable[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> HistogramMetric:
        with self._lock:
            metric = self._histograms.get(name)
            if metric is None:
                metric = HistogramMetric(name, buckets=buckets, description=description)
                self._histograms[name] = metric
            return metric

    # Recording helpers ------------------------------------------------------
    def inc(
        self, name: str, amount: float = 1.0, labels: Optional[Dict[str, str]] = None, description: str = ""
    ) -> None:
        self.counter(name, description=description).inc(amount, labels=labels)

    def set_gauge(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None, description: str = ""
    ) -> None:
        self.gauge(name, description=description).set(value, labels=labels)

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
        buckets: Iterable[float] = DEFAULT_LATENCY_BUCKETS,
        description: str = "",
    ) -> None:
        self.histogram(name, description=description, buckets=buckets).observe(value, labels=labels)

    # Exposition helpers -----------------------------------------------------
    def snapshot(self) -> Dict[str, object]:
        data: Dict[str, object] = {"counters": {}, "gauges": {}, "histograms": {}}
        for name, metric in self._counters.items():
            data["counters"][name] = {
                json.dumps(dict(k), sort_keys=True): v for k, v in metric.samples().items()
            }
        for name, metric in self._gauges.items():
            data["gauges"][name] = {
                json.dumps(dict(k), sort_keys=True): v for k, v in metric.samples().items()
            }
        for name, metric in self._histograms.items():
            hist_snap: Dict[str, object] = {}
            for labels, values in metric.samples().items():
                hist_snap[str(dict(labels))] = values
            data["histograms"][name] = {
                "buckets": metric.buckets,
                "samples": hist_snap,
            }
        return data

    def render_prometheus(self) -> str:
        lines: List[str] = []

        def format_labels(labels: Tuple[Tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            parts = [f'{k}="{v}"' for k, v in labels]
            return "{" + ",".join(parts) + "}"

        for name, metric in self._counters.items():
            if metric.description:
                lines.append(f"# HELP {name} {metric.description}")
            lines.append(f"# TYPE {name} counter")
            for labels, value in metric.samples().items():
                lines.append(f"{name}{format_labels(labels)} {value}")

        for name, metric in self._gauges.items():
            if metric.description:
                lines.append(f"# HELP {name} {metric.description}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in metric.samples().items():
                lines.append(f"{name}{format_labels(labels)} {value}")

        for name, metric in self._histograms.items():
            if metric.description:
                lines.append(f"# HELP {name} {metric.description}")
            lines.append(f"# TYPE {name} histogram")
            for labels, sample in metric.samples().items():
                counts: List[float] = sample["counts"]  # type: ignore[index]
                for bucket, count in zip(metric.buckets, counts):
                    bound_label = dict(labels)
                    bound_label["le"] = str(bucket)
                    lines.append(f"{name}_bucket{format_labels(_labels_key(bound_label))} {count}")
                # +Inf bucket
                bound_label = dict(labels)
                bound_label["le"] = "+inf"
                lines.append(f"{name}_bucket{format_labels(_labels_key(bound_label))} {sample['count']}")  # type: ignore[index]
                lines.append(f"{name}_count{format_labels(labels)} {sample['count']}")  # type: ignore[index]
                lines.append(f"{name}_sum{format_labels(labels)} {sample['sum']}")  # type: ignore[index]

        return "\n".join(lines) + "\n"


_METRICS = MetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    return _METRICS


def set_metrics_registry(registry: MetricsRegistry) -> None:
    global _METRICS
    _METRICS = registry


class _MetricsHandler(BaseHTTPRequestHandler):
    registry: MetricsRegistry
    readiness_fn: Callable[[], bool]
    status_fn: Callable[[], Dict[str, object]]

    def _write(self, status: int, body: str, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._write(200, "ok\n")
            return
        if path == "/ready":
            ready = True
            try:
                ready = self.readiness_fn()
            except Exception:
                ready = False
            status = 200 if ready else 503
            self._write(status, "ready\n" if ready else "not-ready\n")
            return
        if path == "/status":
            payload = {"status": "ok", "metrics": self.registry.snapshot()}
            try:
                payload.update(self.status_fn() or {})
            except Exception:
                payload["status"] = "degraded"
            body = json.dumps(payload, indent=2)
            self._write(200, body, content_type="application/json")
            return
        if path == "/metrics":
            output = self.registry.render_prometheus()
            self._write(200, output, content_type="text/plain; version=0.0.4")
            return
        self._write(404, "not-found\n")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence default HTTP server logging to avoid noisy stdout
        return


def start_metrics_http_server(
    host: str,
    port: int,
    registry: Optional[MetricsRegistry] = None,
    readiness_fn: Optional[Callable[[], bool]] = None,
    status_fn: Optional[Callable[[], Dict[str, object]]] = None,
) -> ThreadingHTTPServer:
    registry = registry or get_metrics_registry()
    readiness_fn = readiness_fn or (lambda: True)
    status_fn = status_fn or (lambda: {})

    handler_class = type(
        "BridgeMetricsHandler",
        (_MetricsHandler,),
        {
            "registry": registry,
            "readiness_fn": staticmethod(readiness_fn),
            "status_fn": staticmethod(status_fn),
        },
    )

    server = ThreadingHTTPServer((host, port), handler_class)

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="metrics-http-server")
    thread.start()
    return server
