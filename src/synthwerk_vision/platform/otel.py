"""OpenTelemetry setup (conventions §4): resource, traces, metrics and logs over OTLP.

The providers are built per app and passed to the instrumentation explicitly; nothing is
set globally, so tests can build many apps in one process.
"""

import logging
import os
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from synthwerk_vision import __version__
from synthwerk_vision.platform.config import Settings
from synthwerk_vision.platform.log import install_handler, remove_handler

logger = logging.getLogger("synthwerk_vision")

SERVICE_NAME = "synthwerk-vision"
HEALTH_PATHS = ("/healthz", "/readyz")
# The log line and the stdout JSON carry these resource attributes.
LOG_RESOURCE_KEYS = ("service.name", "service.version", "deployment.environment.name")

# Emit the stable HTTP semantic conventions (`http.server.request.duration`, `http.route`).
# The instrumentation reads this once, on first use; an explicit value from the env wins.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")


def build_resource(settings: Settings) -> Resource:
    """Service defaults, overridden by `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`."""
    defaults = Resource(
        {
            "service.name": SERVICE_NAME,
            "service.version": __version__,
            "service.namespace": "synthwerk",
            "deployment.environment.name": settings.platform_env,
            "service.instance.id": uuid.uuid4().hex,
        }
    )
    return Resource.create().merge(defaults).merge(OTELResourceDetector().detect())


def log_fields(resource: Resource) -> dict[str, str]:
    return {key: str(resource.attributes[key]) for key in LOG_RESOURCE_KEYS}


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None = None
    _log_handler: logging.Handler | None = field(default=None, init=False)

    def instrument(self, app: FastAPI) -> None:
        if self.logger_provider is not None:
            self._log_handler = LoggingHandler(logger_provider=self.logger_provider)
            install_handler(self._log_handler)
        excluded = os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "")
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            excluded_urls=",".join(filter(None, [excluded, *HEALTH_PATHS])),
            exclude_spans=["receive", "send"],
        )

    def shutdown(self) -> None:
        """Flush the last spans, metrics and logs."""
        if self._log_handler is not None:
            remove_handler(self._log_handler)
            self._log_handler = None
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()


def setup_telemetry(resource: Resource) -> Telemetry:
    """OTLP exporters when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else none and one warning.

    Without an endpoint the tracer provider still makes spans: they give log lines a
    `trace_id` and keep `traceparent` propagation working, but nothing leaves the process.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        logger.warning(
            "telemetry export disabled: OTEL_EXPORTER_OTLP_ENDPOINT is not set",
            extra={"event.name": "otel.export.disabled"},
        )
        return Telemetry(TracerProvider(resource=resource), MeterProvider(resource=resource))

    # The exporters read the endpoint, protocol options and headers from OTEL_* themselves.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    return Telemetry(tracer_provider, meter_provider, logger_provider)
