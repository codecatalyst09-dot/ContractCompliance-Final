import os
import time
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Sequence
from datetime import datetime, timezone

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.trace import Status, StatusCode

from src.config import config

class InMemorySpanExporter(SpanExporter):
    """In-memory exporter for testing and local span inspection."""
    def __init__(self, max_spans: int = 500):
        self._spans: List[ReadableSpan] = []
        self._max_spans = max_spans

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._spans.extend(spans)
        if len(self._spans) > self._max_spans:
            self._spans = self._spans[-self._max_spans:]
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def get_finished_spans(self) -> List[ReadableSpan]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()

# Setup Local In-Memory Tracer & Meter Providers
_in_memory_span_exporter = InMemorySpanExporter()
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(_in_memory_span_exporter))

# Optional Azure Monitor / Application Insights Integration
_azure_monitor_enabled = False
conn_str = config.applicationinsights_connection_string

if conn_str:
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        azure_trace_exporter = AzureMonitorTraceExporter.from_connection_string(conn_str)
        _tracer_provider.add_span_processor(BatchSpanProcessor(azure_trace_exporter))
        _azure_monitor_enabled = True
    except ImportError:
        pass
    except Exception as e:
        print(f"[Telemetry Warning] Could not initialize Azure Monitor exporter: {e}")

trace.set_tracer_provider(_tracer_provider)
tracer = trace.get_tracer("contract-compliance-agent", "1.0.0")

_meter_provider = MeterProvider()
metrics.set_meter_provider(_meter_provider)
meter = metrics.get_meter("contract-compliance-agent", "1.0.0")

# ── Defined Telemetry Metrics ──────────────────────────────────────────────────
workflow_counter = meter.create_counter(
    name="contracts_processed_total",
    description="Total number of contracts processed through compliance workflow",
    unit="1"
)

contracts_successful_counter = meter.create_counter(
    name="contracts_successful_total",
    description="Total number of contracts successfully evaluated",
    unit="1"
)

contracts_failed_counter = meter.create_counter(
    name="contracts_failed_total",
    description="Total number of contracts that encountered workflow failures",
    unit="1"
)

contracts_by_status_counter = meter.create_counter(
    name="contracts_by_status_total",
    description="Contracts broken down by compliance status (PASS, RISK, FAIL, SKIPPED)",
    unit="1"
)

contracts_by_risk_counter = meter.create_counter(
    name="contracts_by_risk_level_total",
    description="Contracts broken down by risk tier (LOW, MEDIUM, HIGH, CRITICAL)",
    unit="1"
)

agent_executions_counter = meter.create_counter(
    name="agent_executions_total",
    description="Total executions per individual AI compliance agent",
    unit="1"
)

workflow_duration_histogram = meter.create_histogram(
    name="compliance_workflow_duration_ms",
    description="Duration of compliance workflow execution in milliseconds",
    unit="ms"
)

stage_duration_histogram = meter.create_histogram(
    name="compliance_stage_duration_ms",
    description="Duration of individual workflow stage execution in milliseconds",
    unit="ms"
)

risk_score_histogram = meter.create_histogram(
    name="compliance_contract_risk_score",
    description="Distribution of contract risk scores (0-100)",
    unit="score"
)

def is_azure_monitor_enabled() -> bool:
    return _azure_monitor_enabled

def get_recorded_spans() -> List[ReadableSpan]:
    return _in_memory_span_exporter.get_finished_spans()

def clear_recorded_spans():
    _in_memory_span_exporter.clear()

def record_contract_metrics(
    run_id: str,
    status: str,
    risk_level: Optional[str] = None,
    risk_score: Optional[float] = None,
    duration_ms: Optional[float] = None
):
    """Record high-level contract evaluation metrics."""
    workflow_counter.add(1, {"status": status})
    if status == "FAILED":
        contracts_failed_counter.add(1)
    else:
        contracts_successful_counter.add(1)

    contracts_by_status_counter.add(1, {"status": status})

    if risk_level:
        contracts_by_risk_counter.add(1, {"risk_level": risk_level})
    if risk_score is not None:
        risk_score_histogram.record(risk_score, {"risk_level": risk_level or "UNKNOWN"})
    if duration_ms is not None:
        workflow_duration_histogram.record(duration_ms, {"status": status})

def record_agent_stage_metrics(
    stage_name: str,
    agent_name: Optional[str],
    status: str,
    duration_ms: float
):
    """Record individual AI agent stage latency and execution counters."""
    agent_executions_counter.add(1, {
        "stage": stage_name,
        "agent": agent_name or "WorkflowCore",
        "status": status
    })
    stage_duration_histogram.record(duration_ms, {
        "stage": stage_name,
        "agent": agent_name or "WorkflowCore",
        "status": status
    })

@contextmanager
def trace_workflow_span(run_id: str, file_path: str):
    """Context manager tracing full workflow execution with OpenTelemetry."""
    t0 = time.time()
    with tracer.start_as_current_span("compliance_workflow_execution") as span:
        span.set_attribute("workflow.run_id", run_id)
        span.set_attribute("workflow.file_path", file_path)
        span.set_attribute("workflow.environment", config.environment)
        span.set_attribute("service.name", "contract-compliance-agent")
        try:
            yield span
            duration_ms = (time.time() - t0) * 1000
            span.set_attribute("workflow.status", "SUCCESS")
            span.set_attribute("workflow.duration_ms", duration_ms)
            span.set_status(Status(StatusCode.OK))
            record_contract_metrics(run_id=run_id, status="SUCCESS", duration_ms=duration_ms)
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            span.set_attribute("workflow.status", "FAILED")
            span.set_attribute("workflow.duration_ms", duration_ms)
            span.set_attribute("workflow.error_type", type(e).__name__)
            span.set_attribute("workflow.error_message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            record_contract_metrics(run_id=run_id, status="FAILED", duration_ms=duration_ms)
            raise

@contextmanager
def trace_stage_span(stage_name: str, agent_name: Optional[str], run_id: str):
    """Context manager tracing an individual workflow stage."""
    t0 = time.time()
    with tracer.start_as_current_span(f"stage_{stage_name}") as span:
        span.set_attribute("stage.name", stage_name)
        span.set_attribute("stage.run_id", run_id)
        span.set_attribute("service.name", "contract-compliance-agent")
        if agent_name:
            span.set_attribute("stage.agent_name", agent_name)
        try:
            yield span
            duration_ms = (time.time() - t0) * 1000
            span.set_attribute("stage.status", "SUCCESS")
            span.set_attribute("stage.duration_ms", duration_ms)
            span.set_status(Status(StatusCode.OK))
            record_agent_stage_metrics(stage_name, agent_name, "SUCCESS", duration_ms)
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            span.set_attribute("stage.status", "FAILED")
            span.set_attribute("stage.duration_ms", duration_ms)
            span.set_attribute("stage.error_type", type(e).__name__)
            span.set_attribute("stage.error_message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            record_agent_stage_metrics(stage_name, agent_name, "FAILED", duration_ms)
            raise
