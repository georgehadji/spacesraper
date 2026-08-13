# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (OpenTelemetry Observability)
# Role: Distributed tracing, metrics, and structured logging.

import logging
import sys
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from typing import Any

# OpenTelemetry imports
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.config_settings import settings

# Prometheus export and the logging instrumentor ship only in
# requirements-enterprise.txt, so neither import may break a base install.
try:
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
except ImportError:  # pragma: no cover - depends on the installed extras
    PrometheusMetricReader = None

try:
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
except ImportError:  # pragma: no cover - depends on the installed extras
    LoggingInstrumentor = None

logger = logging.getLogger("Spacescraper.Observability")

# Trace/Span context
from opentelemetry.trace import SpanKind, Status, StatusCode


class ObservabilityManager:
    """
    Spacescraper Observability Hub.
    Manages distributed tracing, metrics, and structured logging.
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._tracer: trace.Tracer | None = None
        self._meter: metrics.Meter | None = None
        self._logger: logging.Logger | None = None
        self._resource = Resource.create({
            SERVICE_NAME: settings.observability.service_name,
            SERVICE_VERSION: settings.observability.service_version,
            "deployment.environment": settings.environment,
        })
        
    def initialize(self):
        """Initialize OpenTelemetry providers."""
        if self._initialized:
            return
        
        # Initialize tracing
        if settings.observability.tracing_enabled:
            self._init_tracing()
        
        # Initialize metrics
        if settings.observability.metrics_enabled:
            self._init_metrics()
        
        # Initialize logging
        self._init_logging()
        
        self._initialized = True
        self._logger.info("ObservabilityManager initialized")

    def _init_tracing(self):
        """Initialize distributed tracing."""
        provider = TracerProvider(resource=self._resource)
        
        # OTLP exporter for Jaeger/Tempo
        if settings.observability.exporter_endpoint:
            otlp_exporter = OTLPSpanExporter(
                endpoint=settings.observability.exporter_endpoint,
                insecure=True
            )
            provider.add_span_processor(
                BatchSpanProcessor(otlp_exporter)
            )
        
        # Console exporter for development
        if settings.debug:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )
        
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer(__name__)

    def _init_metrics(self):
        """Initialize metrics collection."""
        readers = []
        
        # Prometheus endpoint
        if PrometheusMetricReader is None:
            logger.info("Prometheus metrics not available: exporter package not installed.")
        else:
            try:
                readers.append(PrometheusMetricReader())
            except Exception as e:
                logger.warning("Prometheus metrics not available: %s", e)
        
        # OTLP exporter
        if settings.observability.exporter_endpoint:
            otlp_exporter = OTLPMetricExporter(
                endpoint=settings.observability.exporter_endpoint,
                insecure=True
            )
            readers.append(PeriodicExportingMetricReader(otlp_exporter))
        
        provider = MeterProvider(resource=self._resource, metric_readers=readers)
        metrics.set_meter_provider(provider)
        self._meter = metrics.get_meter(__name__)
        
        # Create common metrics
        self._create_metrics()

    def _create_metrics(self):
        """Create application-specific metrics."""
        if not self._meter:
            return
            
        # Counters
        self.jobs_submitted = self._meter.create_counter(
            "scraper.jobs.submitted",
            description="Total jobs submitted"
        )
        self.jobs_completed = self._meter.create_counter(
            "scraper.jobs.completed",
            description="Total jobs completed successfully"
        )
        self.jobs_failed = self._meter.create_counter(
            "scraper.jobs.failed",
            description="Total jobs failed"
        )
        self.pages_scraped = self._meter.create_counter(
            "scraper.pages.scraped",
            description="Total pages scraped"
        )
        
        # Histograms
        self.job_duration = self._meter.create_histogram(
            "scraper.job.duration",
            description="Job processing duration in ms",
            unit="ms"
        )
        self.page_load_time = self._meter.create_histogram(
            "scraper.page.load_time",
            description="Page load time in ms",
            unit="ms"
        )
        
        # UpDown counters
        self.active_jobs = self._meter.create_up_down_counter(
            "scraper.jobs.active",
            description="Number of currently active jobs"
        )

    def _init_logging(self):
        """Initialize structured logging."""
        # Configure root logger
        logging.basicConfig(
            level=getattr(logging, settings.observability.log_level.upper()),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        
        if settings.observability.json_logs:
            # JSON structured logging
            try:
                import pythonjsonlogger.jsonlogger
                log_handler = logging.StreamHandler(sys.stdout)
                formatter = pythonjsonlogger.jsonlogger.JsonFormatter(
                    "%(asctime)s %(name)s %(levelname)s %(message)s"
                )
                log_handler.setFormatter(formatter)
                
                # Apply to root logger
                root_logger = logging.getLogger()
                root_logger.handlers = [log_handler]
            except ImportError:
                pass
        
        # Instrument logging with trace context
        if LoggingInstrumentor is not None:
            LoggingInstrumentor().instrument()
        else:
            logger.info("Log/trace correlation disabled: instrumentation package not installed.")
        
        self._logger = logging.getLogger("Spacescraper.Observability")

    def get_tracer(self) -> trace.Tracer:
        """Get the configured tracer."""
        if not self._tracer:
            self._tracer = trace.get_tracer(__name__)
        return self._tracer

    def get_meter(self) -> metrics.Meter:
        """Get the configured meter."""
        if not self._meter:
            self._meter = metrics.get_meter(__name__)
        return self._meter

    # Context managers for tracing
    @contextmanager
    def span(self, name: str, kind: SpanKind = SpanKind.INTERNAL, attributes: dict[str, Any] | None = None):
        """Context manager for creating spans."""
        tracer = self.get_tracer()
        with tracer.start_as_current_span(name, kind=kind) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            yield span

    def trace_method(self, name: str | None = None, attributes: dict[str, Any] | None = None):
        """Decorator to trace method execution."""
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                span_name = name or func.__name__
                with self.span(span_name, attributes=attributes) as span:
                    try:
                        result = await func(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as e:
                        span.set_status(Status(StatusCode.ERROR, str(e)))
                        span.record_exception(e)
                        raise
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                span_name = name or func.__name__
                with self.span(span_name, attributes=attributes) as span:
                    try:
                        result = func(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as e:
                        span.set_status(Status(StatusCode.ERROR, str(e)))
                        span.record_exception(e)
                        raise
            
            return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        return decorator

    def record_job_submitted(self, job_id: str, source: str):
        """Record job submission metric."""
        if hasattr(self, 'jobs_submitted'):
            self.jobs_submitted.add(1, {"source": source})

    def record_job_completed(self, job_id: str, source: str, duration_ms: float):
        """Record job completion metric."""
        if hasattr(self, 'jobs_completed'):
            self.jobs_completed.add(1, {"source": source})
        if hasattr(self, 'job_duration'):
            self.job_duration.record(duration_ms, {"source": source})

    def record_job_failed(self, job_id: str, source: str, error_type: str):
        """Record job failure metric."""
        if hasattr(self, 'jobs_failed'):
            self.jobs_failed.add(1, {"source": source, "error_type": error_type})

    def record_page_scraped(self, url: str, status_code: int, load_time_ms: float):
        """Record page scrape metric."""
        if hasattr(self, 'pages_scraped'):
            self.pages_scraped.add(1)
        if hasattr(self, 'page_load_time'):
            self.page_load_time.record(load_time_ms)


import asyncio

# Global instance
observability = ObservabilityManager()


# Convenience functions for common patterns
def trace_span(name: str, kind: SpanKind = SpanKind.INTERNAL):
    """Decorator for tracing functions."""
    return observability.trace_method(name)


class TracedContext:
    """Async context manager for tracing blocks of code."""
    
    def __init__(self, name: str, attributes: dict[str, Any] | None = None):
        self.name = name
        self.attributes = attributes
        self.span = None
    
    async def __aenter__(self):
        tracer = observability.get_tracer()
        self.span = tracer.start_span(self.name)
        if self.attributes:
            for key, value in self.attributes.items():
                self.span.set_attribute(key, value)
        return self.span
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            self.span.set_status(Status(StatusCode.ERROR, str(exc_val)))
            self.span.record_exception(exc_val)
        else:
            self.span.set_status(Status(StatusCode.OK))
        self.span.end()
