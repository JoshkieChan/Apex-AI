"""OpenTelemetry-compatible spans. No collector is required for local use."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_CONFIGURED = False


def setup_tracing(exporter: InMemorySpanExporter | None = None) -> TracerProvider | None:
    """Install a tracer provider once. Tests may pass an in-memory exporter."""
    global _CONFIGURED
    provider = trace.get_tracer_provider()
    if _CONFIGURED and isinstance(provider, TracerProvider):
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider
    if isinstance(provider, TracerProvider):
        _CONFIGURED = True
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider

    created = TracerProvider(resource=Resource.create({"service.name": "apex-ai"}))
    if exporter is not None:
        created.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(created)
    _CONFIGURED = True
    return created


@contextmanager
def traced(name: str, **attributes: str) -> Iterator[trace.Span]:
    """Start a span. Attribute values must already be non-sensitive identifiers."""
    tracer = trace.get_tracer("apex")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        yield span
