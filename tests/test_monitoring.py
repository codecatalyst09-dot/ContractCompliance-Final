import os
import json
import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.monitoring.telemetry import (
    get_recorded_spans,
    clear_recorded_spans,
    is_azure_monitor_enabled,
    record_contract_metrics,
    record_agent_stage_metrics
)
from src.monitoring.logging_config import sanitize_log_payload, log_event, get_logger

client = TestClient(app)

def test_monitoring_overview_endpoint():
    response = client.get("/api/monitoring/overview")
    assert response.status_code == 200
    data = response.json()
    assert "total_contracts" in data
    assert "successful" in data
    assert "failed" in data
    assert "success_rate" in data
    assert "average_processing_time" in data
    assert "high_risk" in data
    assert "azure_monitor_enabled" in data

def test_monitoring_agents_endpoint():
    response = client.get("/api/monitoring/agents")
    assert response.status_code == 200
    data = response.json()
    assert "agents" in data
    agents = data["agents"]
    assert len(agents) >= 6
    agent_ids = [a["id"] for a in agents]
    assert "document_classifier" in agent_ids
    assert "obligation_agent" in agent_ids
    assert "template_agent" in agent_ids
    assert "policy_agent" in agent_ids
    assert "validation_agent" in agent_ids
    assert "risk_engine" in agent_ids
    for a in agents:
        assert a["status"] in ["healthy", "degraded", "unhealthy"]
        assert "avg_duration_ms" in a
        assert "success_rate" in a

def test_monitoring_executions_endpoint():
    response = client.get("/api/monitoring/executions?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "executions" in data
    assert isinstance(data["executions"], list)

def test_monitoring_errors_endpoint():
    response = client.get("/api/monitoring/errors?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "errors" in data
    assert isinstance(data["errors"], list)

def test_monitoring_kql_queries_endpoint():
    response = client.get("/api/monitoring/kql-queries")
    assert response.status_code == 200
    data = response.json()
    assert "queries" in data
    assert len(data["queries"]) == 10
    first_query = data["queries"][0]
    assert "title" in first_query
    assert "kql" in first_query
    assert len(first_query["kql"]) > 10

def test_sanitization_of_sensitive_data():
    raw_data = {
        "api_key": "secret_12345",
        "password": "my_super_secret_password",
        "connection_string": "InstrumentationKey=xyz",
        "document_text": "CONFIDENTIAL COMPANY TRADE SECRET"
    }
    cleaned = sanitize_log_payload(raw_data)
    assert cleaned["api_key"] == "[REDACTED_SECRET]"
    assert cleaned["password"] == "[REDACTED_SECRET]"
    assert cleaned["connection_string"] == "[REDACTED_SECRET]"
    assert "[REDACTED_DOCUMENT_TEXT" in cleaned["document_text"]

def test_telemetry_metrics_recording():
    record_contract_metrics(
        run_id="test-run-123",
        status="SUCCESS",
        risk_level="HIGH",
        risk_score=75,
        duration_ms=2500
    )
    record_agent_stage_metrics(
        stage_name="classification",
        agent_name="ClassificationAgent",
        status="SUCCESS",
        duration_ms=1200
    )

def test_monitor_page_html_serving():
    response = client.get("/monitor")
    assert response.status_code == 200
    assert "Live Telemetry & System Monitor" in response.text
    assert "AI Compliance Agent Health & Latency Grid" in response.text
    assert "Recent Contract Compliance Executions" in response.text
