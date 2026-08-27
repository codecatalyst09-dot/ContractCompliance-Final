"""
FastAPI Web Application — Contract Compliance Agent Dashboard
"""

import os
import sys
import json
import uuid
import shutil
import asyncio
from pathlib import Path
from typing import List, Optional
from datetime import datetime

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from src.database.db import (
    init_db, create_run, update_run_from_result, mark_run_failed,
    get_all_runs, get_run, delete_run, get_stats,
    get_flagged_runs, set_admin_decision, get_admin_stats
)
from src.workflow.compliance_workflow import ContractComplianceWorkflow, get_compliance_workflow
from src.monitoring.logging_config import get_logger

logger = get_logger("api")

# ── Ensure required output directories exist ──────────────────────────────────
for d in ["uploads", "outputs/compliance", "outputs/evidence", "outputs/evidence_images", "outputs/audit", "database"]:
    os.makedirs(d, exist_ok=True)

# ── Initialise SQLite DB ──────────────────────────────────────────────────────
init_db()

# ── Project paths ─────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
POLICIES_DIR = ROOT_DIR / "policies"
static_dir = Path(__file__).parent.parent / "static"

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="Contract Compliance Agent", version="1.0.0")

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ── WebSocket Manager ─────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, run_id: str, ws: WebSocket):
        await ws.accept()
        if run_id not in self.active:
            self.active[run_id] = []
        self.active[run_id].append(ws)

    def disconnect(self, run_id: str, ws: Optional[WebSocket] = None):
        if run_id in self.active:
            if ws and ws in self.active[run_id]:
                self.active[run_id].remove(ws)
            if not ws or not self.active[run_id]:
                self.active.pop(run_id, None)

    async def send(self, run_id: str, data: dict):
        sockets = list(self.active.get(run_id, []))
        for ws in sockets:
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(run_id, ws)

manager = ConnectionManager()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index_path = static_dir / "index.html"
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/monitor", response_class=HTMLResponse)
async def serve_monitor():
    monitor_path = static_dir / "monitor.html"
    if monitor_path.exists():
        return HTMLResponse(content=monitor_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Monitor</h1>")


@app.get("/api/stats")
async def api_stats():
    return get_stats()


@app.get("/api/runs")
async def api_list_runs(limit: int = 200, offset: int = 0):
    return get_all_runs(limit=limit, offset=offset)


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: str):
    deleted = delete_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"deleted": True, "run_id": run_id}


@app.get("/api/runs/{run_id}/report")
async def api_get_report(run_id: str):
    path = f"outputs/compliance/{run_id}_report.md"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="text/markdown", filename=f"{run_id}_report.md")


@app.get("/api/runs/{run_id}/document")
async def api_get_original_document(run_id: str):
    """Download the exact original contract file uploaded by the user."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    file_path = run.get("file_path")
    if not file_path or not os.path.exists(file_path):
        # Fallback to search in uploads or documents directory
        file_name = run.get("file_name", "")
        candidates = list(Path("uploads").glob(f"*{file_name}*")) + list(Path("documents").glob(f"*{file_name}*"))
        if candidates and candidates[0].is_file():
            file_path = str(candidates[0])
        else:
            raise HTTPException(status_code=404, detail="Original document file not found on server")
    
    file_name = run.get("file_name") or Path(file_path).name
    ext = Path(file_path).suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain"
    }
    media_type = media_types.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type, filename=file_name, content_disposition_type="inline")


@app.get("/api/runs/{run_id}/document-text")
async def api_get_document_text(run_id: str):
    """Return extracted text content with page-level segments for read-only preview."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    file_path = run.get("file_path")
    if not file_path or not os.path.exists(file_path):
        file_name = run.get("file_name", "")
        candidates = list(Path("uploads").glob(f"*{file_name}*")) + list(Path("documents").glob(f"*{file_name}*"))
        if candidates and candidates[0].is_file():
            file_path = str(candidates[0])
        else:
            file_path = ""

    ext = Path(file_path).suffix.lower() if file_path else (Path(run.get("file_name", "")).suffix.lower())
    pages_data = []

    if file_path and os.path.exists(file_path):
        if ext == ".txt":
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    pages_data.append({"page_number": 1, "text": f.read()})
            except Exception:
                pages_data.append({"page_number": 1, "text": "Could not read text file."})
        elif ext == ".pdf":
            try:
                import fitz
                doc = fitz.open(file_path)
                for idx, page in enumerate(doc):
                    pages_data.append({"page_number": idx + 1, "text": page.get_text()})
            except Exception:
                pass
        elif ext in [".docx", ".doc"]:
            try:
                from src.ingestion.docx_extractor import extract_docx
                pages, full_text, tables = extract_docx(file_path)
                if pages:
                    for p in pages:
                        pages_data.append({"page_number": p.page_number, "text": p.text})
                elif full_text:
                    pages_data.append({"page_number": 1, "text": full_text})
            except Exception:
                try:
                    import docx
                    d = docx.Document(file_path)
                    paragraphs = [p.text for p in d.paragraphs if p.text.strip()]
                    full_text = "\n\n".join(paragraphs)
                    pages_data.append({"page_number": 1, "text": full_text})
                except Exception:
                    pass

    # Fallback to saved audit JSON if file is missing or extraction was empty
    if not pages_data:
        audit_path = f"outputs/audit/{run_id}_audit.json"
        comp_path = f"outputs/compliance/{run_id}_compliance.json"
        for p in [audit_path, comp_path]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        doc_data = data.get("document", {})
                        raw_pages = doc_data.get("pages", [])
                        if raw_pages:
                            for idx, pg in enumerate(raw_pages):
                                pages_data.append({
                                    "page_number": pg.get("page_number", idx + 1),
                                    "text": pg.get("text", "")
                                })
                        elif doc_data.get("text"):
                            pages_data.append({"page_number": 1, "text": doc_data.get("text")})
                    if pages_data:
                        break
                except Exception:
                    pass

    return {
        "run_id": run_id,
        "file_name": run.get("file_name"),
        "file_type": ext.replace(".", "") if ext else "unknown",
        "total_pages": len(pages_data),
        "pages": pages_data
    }


@app.get("/api/runs/{run_id}/page-image/{page_number}")
async def api_get_page_image(run_id: str, page_number: int):
    """Render a specific PDF page as a high-resolution JPEG image for authentic document view."""
    from fastapi.responses import Response
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    file_path = run.get("file_path")
    if not file_path or not os.path.exists(file_path):
        file_name = run.get("file_name", "")
        candidates = list(Path("uploads").glob(f"*{file_name}*")) + list(Path("documents").glob(f"*{file_name}*"))
        if candidates and candidates[0].is_file():
            file_path = str(candidates[0])
        else:
            raise HTTPException(status_code=404, detail="Original document not found")

    ext = Path(file_path).suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Page images are only available for PDF documents")

    try:
        import fitz
        doc = fitz.open(file_path)
        if page_number < 1 or page_number > len(doc):
            raise HTTPException(status_code=400, detail=f"Invalid page number. Document has {len(doc)} pages.")
        
        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("jpeg")
        return Response(content=img_bytes, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to render page: {str(e)}")


@app.get("/api/evidence/{run_id}/{policy_id}")
async def api_get_evidence_image(run_id: str, policy_id: str):
    normalized = policy_id.replace("-", "_").replace(" ", "_").upper()
    path = f"outputs/evidence_images/{run_id}_{normalized}_evidence.jpg"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Evidence image not found")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/policies")
async def api_list_policies():
    """Return list of policy files available on the server."""
    found = []
    for d in [POLICIES_DIR, Path("policies")]:
        if d.exists() and d.is_dir():
            for f in d.glob("*.json"):
                if f.name not in found:
                    found.append(f.name)
    if not found and (POLICIES_DIR / "policies.json").exists():
        found.append("policies.json")
    return {"policy_files": sorted(found)}


@app.get("/logs", response_class=HTMLResponse)
@app.get("/monitor", response_class=HTMLResponse)
async def serve_logs_page():
    monitor_path = static_dir / "monitor.html"
    return HTMLResponse(content=monitor_path.read_text(encoding="utf-8"))


@app.get("/api/logs")
async def api_get_logs(limit: int = 200, run_id: Optional[str] = None, level: Optional[str] = None):
    """Return structured log entries, optionally filtered by run_id and/or level."""
    from collections import deque
    log_path = "logs/application.jsonl"
    if not os.path.exists(log_path):
        return {"logs": []}
    lines: deque = deque(maxlen=max(1, limit))
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    # Filter by run_id if provided
                    if run_id and entry.get("run_id") != run_id:
                        continue
                    # Filter by level if provided
                    if level and entry.get("level", "").upper() != level.upper():
                        continue
                    lines.append(entry)
                except Exception:
                    if not run_id and not level:
                        lines.append({"message": line, "level": "INFO"})
    return {"logs": list(lines)}


@app.delete("/api/logs")
@app.post("/api/logs/clear")
async def api_clear_logs():
    """Clear all application activity logs."""
    log_path = "logs/application.jsonl"
    try:
        if os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("")
        return {"status": "success", "message": "All activity logs cleared successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear logs: {str(e)}")


# ── Azure Monitor & Telemetry APIs ───────────────────────────────────────────

@app.get("/api/monitoring/overview")
async def api_monitoring_overview():
    """Return high-level application & telemetry metrics for monitoring dashboard."""
    from src.monitoring.telemetry import is_azure_monitor_enabled
    from src.config import config
    
    runs = get_all_runs(limit=1000)
    total = len(runs)
    if total == 0:
        return {
            "total_contracts": 0,
            "successful": 0,
            "failed": 0,
            "success_rate": 0.0,
            "average_processing_time": 0.0,
            "high_risk": 0,
            "medium_risk": 0,
            "low_risk": 0,
            "compliant": 0,
            "non_compliant": 0,
            "azure_monitor_enabled": is_azure_monitor_enabled(),
            "environment": config.environment or "development"
        }

    failed = sum(1 for r in runs if r.get("processing_status") == "FAILED")
    successful = total - failed
    success_rate = round((successful / total) * 100, 1) if total > 0 else 0.0

    high_risk = sum(1 for r in runs if (r.get("risk_level") or "").upper() in ["HIGH", "CRITICAL"])
    medium_risk = sum(1 for r in runs if (r.get("risk_level") or "").upper() == "MEDIUM")
    low_risk = sum(1 for r in runs if (r.get("risk_level") or "").upper() == "LOW")

    compliant = sum(1 for r in runs if (r.get("overall_status") or "").upper() in ["PASS", "APPROVED"])
    non_compliant = sum(1 for r in runs if (r.get("overall_status") or "").upper() in ["RISK", "FAIL", "REJECTED"])

    # Calculate average processing time from completed runs
    durations = []
    for r in runs:
        c_at = r.get("created_at")
        done_at = r.get("completed_at")
        if c_at and done_at:
            try:
                t0 = datetime.fromisoformat(c_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(done_at.replace("Z", "+00:00"))
                diff = (t1 - t0).total_seconds()
                if 0 < diff < 3600:
                    durations.append(diff)
            except Exception:
                pass
    
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0

    return {
        "total_contracts": total,
        "successful": successful,
        "failed": failed,
        "success_rate": success_rate,
        "average_processing_time": avg_duration,
        "high_risk": high_risk,
        "medium_risk": medium_risk,
        "low_risk": low_risk,
        "compliant": compliant,
        "non_compliant": non_compliant,
        "azure_monitor_enabled": is_azure_monitor_enabled(),
        "environment": config.environment or "development"
    }


@app.get("/api/monitoring/agents")
async def api_monitoring_agents():
    """Return health, latency, and operational status for all AI compliance agents."""
    agents_def = [
        {"id": "document_classifier", "name": "Document Classifier", "agent_class": "ClassificationAgent", "stage": "classification", "icon": "🏷️"},
        {"id": "obligation_agent", "name": "Obligation Extraction Agent", "agent_class": "ObligationExtractionAgent", "stage": "obligation_extraction", "icon": "📌"},
        {"id": "template_agent", "name": "Template Structure Checker", "agent_class": "TemplateChecker", "stage": "template_check", "icon": "📐"},
        {"id": "policy_agent", "name": "Policy Clause Matching Agent", "agent_class": "PolicyClauseMatchingAgent", "stage": "policy_matching", "icon": "🔍"},
        {"id": "validation_agent", "name": "Compliance Validation Agent", "agent_class": "ComplianceValidationAgent", "stage": "compliance_validation", "icon": "✅"},
        {"id": "risk_engine", "name": "Deterministic Risk Engine", "agent_class": "DeterministicRiskEngine", "stage": "risk_scoring", "icon": "📊"},
        {"id": "evidence_agent", "name": "Visual Evidence Highlight Agent", "agent_class": "EvidenceGenerationAgent", "stage": "evidence_generation", "icon": "📸"}
    ]

    # Read structured log file to calculate real latency & error stats
    log_path = "logs/application.jsonl"
    agent_stats = {a["stage"]: {"calls": 0, "durations": [], "errors": 0} for a in agents_def}

    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    stage = e.get("stage")
                    if stage in agent_stats:
                        dur = e.get("duration_ms")
                        # Count completed stage invocations
                        if dur is not None and dur > 0:
                            agent_stats[stage]["calls"] += 1
                            agent_stats[stage]["durations"].append(dur)
                        elif e.get("status") in ["DONE", "COMPLETED"]:
                            agent_stats[stage]["calls"] += 1
                            
                        if (e.get("level") or "").upper() == "ERROR" or e.get("error_message") or e.get("status") == "FAILED":
                            agent_stats[stage]["errors"] += 1
                except Exception:
                    pass

    result = []
    for a in agents_def:
        st = agent_stats.get(a["stage"], {"calls": 0, "durations": [], "errors": 0})
        total_calls = st["calls"]
        errors = st["errors"]
        durs = st["durations"]
        avg_ms = round(sum(durs) / len(durs), 1) if durs else 0.0
        
        if total_calls == 0:
            success_rate = 100.0
            status = "healthy"
        else:
            success_rate = round((max(0, total_calls - errors) / total_calls) * 100, 1)
            status = "healthy"
            if errors > 0 and success_rate < 80:
                status = "degraded"
            elif errors > 3 and success_rate < 50:
                status = "unhealthy"

        result.append({
            "id": a["id"],
            "name": a["name"],
            "agent_class": a["agent_class"],
            "stage": a["stage"],
            "icon": a["icon"],
            "status": status,
            "success_rate": success_rate,
            "avg_duration_ms": avg_ms,
            "total_calls": total_calls,
            "recent_errors": errors
        })

    return {"agents": result}


@app.get("/api/monitoring/executions")
async def api_monitoring_executions(limit: int = 50):
    """Return recent contract compliance executions with execution ID, duration, and status."""
    runs = get_all_runs(limit=limit)
    executions = []
    for r in runs:
        duration_sec = None
        c_at = r.get("created_at")
        done_at = r.get("completed_at")
        if c_at and done_at:
            try:
                t0 = datetime.fromisoformat(c_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(done_at.replace("Z", "+00:00"))
                duration_sec = round((t1 - t0).total_seconds(), 2)
            except Exception:
                pass

        executions.append({
            "execution_id": r.get("run_id"),
            "document_name": r.get("file_name"),
            "document_type": r.get("document_type") or "Contract",
            "status": r.get("processing_status"),
            "compliance_status": r.get("overall_status") or r.get("processing_status"),
            "risk_score": r.get("risk_score"),
            "risk_level": r.get("risk_level") or "LOW",
            "duration_seconds": duration_sec,
            "created_at": r.get("created_at"),
            "completed_at": r.get("completed_at")
        })

    return {"executions": executions}


@app.get("/api/monitoring/errors")
async def api_monitoring_errors(limit: int = 50):
    """Return recent application errors and exceptions with execution ID and agent context."""
    log_path = "logs/application.jsonl"
    if not os.path.exists(log_path):
        return {"errors": []}

    errors = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                lvl = (e.get("level") or "").upper()
                if lvl in ["ERROR", "CRITICAL", "WARNING"] or e.get("error_message") or e.get("error_type"):
                    errors.append({
                        "timestamp": e.get("timestamp"),
                        "execution_id": e.get("run_id") or "SYSTEM",
                        "stage": e.get("stage") or "system",
                        "agent": e.get("agent") or e.get("logger") or "System",
                        "level": lvl,
                        "error_type": e.get("error_type") or ("Warning" if lvl == "WARNING" else "Error"),
                        "error_message": e.get("error_message") or e.get("message")
                    })
            except Exception:
                pass

    return {"errors": list(reversed(errors))[:limit]}


@app.get("/api/monitoring/kql-queries")
async def api_monitoring_kql_queries():
    """Return production-ready Azure Monitor KQL queries for Application Insights & Log Analytics."""
    return {
        "queries": [
            {
                "title": "1. Recent Contract Compliance Requests",
                "description": "Lists all incoming HTTP API requests with status codes and duration.",
                "kql": "AppRequests\n| where TimeGenerated > ago(24h)\n| project TimeGenerated, Name, Url, ResultCode, DurationMs, Success\n| order by TimeGenerated desc"
            },
            {
                "title": "2. Failed API Requests & Errors",
                "description": "Inspects API requests that returned 4xx or 5xx status codes.",
                "kql": "AppRequests\n| where Success == false or ResultCode >= 400\n| project TimeGenerated, Name, ResultCode, DurationMs, Url\n| order by TimeGenerated desc"
            },
            {
                "title": "3. Unhandled Exceptions & Crash Telemetry",
                "description": "Shows exceptions captured during contract workflow executions.",
                "kql": "AppExceptions\n| where TimeGenerated > ago(24h)\n| project TimeGenerated, ProblemId, ExceptionType, OuterMessage, Method\n| order by TimeGenerated desc"
            },
            {
                "title": "4. Contract Compliance Agent Traces",
                "description": "Traces individual AI agent events and execution IDs.",
                "kql": "AppTraces\n| where Message has '[contract_compliance]' or CustomDimensions['service.name'] == 'contract-compliance-agent'\n| extend ExecutionId = tostring(CustomDimensions['workflow.run_id'])\n| project TimeGenerated, SeverityLevel, Message, ExecutionId\n| order by TimeGenerated desc"
            },
            {
                "title": "5. AI Agent Execution Duration Breakdown",
                "description": "Calculates average latency and execution time per AI Agent stage.",
                "kql": "AppDependencies\n| where Type in ('InProc', 'AI Agent') or Name startswith 'stage_'\n| summarize AvgDurationMs = avg(DurationMs), P95DurationMs = percentile(DurationMs, 95), TotalCalls = count() by Name\n| order by AvgDurationMs desc"
            },
            {
                "title": "6. Failed Agent Executions & Reasons",
                "description": "Captures any agent stage that encountered an unrecoverable failure.",
                "kql": "AppTraces\n| extend Status = tostring(CustomDimensions['status'])\n| where Status == 'FAILED' or SeverityLevel >= 3\n| project TimeGenerated, Message, Stage=CustomDimensions['stage'], RunId=CustomDimensions['run_id']\n| order by TimeGenerated desc"
            },
            {
                "title": "7. Total Contracts Processed Over Time",
                "description": "Time-series count of contract evaluation workflows.",
                "kql": "AppMetrics\n| where Name in ('compliance_workflow_runs_total', 'contracts_processed_total')\n| summarize TotalContracts = sum(Val) by bin(TimeGenerated, 1h)\n| render timechart"
            },
            {
                "title": "8. High-Risk vs Compliant Contracts",
                "description": "Categorizes contract compliance results into Risk tiers.",
                "kql": "AppMetrics\n| where Name == 'compliance_contract_risk_score'\n| extend RiskTier = case(Val >= 50, 'High Risk', Val >= 20, 'Medium Risk', 'Low Risk')\n| summarize Count = count() by RiskTier"
            },
            {
                "title": "9. Slowest Contract Executions",
                "description": "Identifies the slowest contracts analyzed to optimize agent throughput.",
                "kql": "AppDependencies\n| where Name == 'compliance_workflow_execution'\n| project TimeGenerated, ExecutionId = tostring(CustomDimensions['workflow.run_id']), DurationSec = DurationMs / 1000.0\n| top 20 by DurationSec desc"
            },
            {
                "title": "10. Application Insights Live Availability",
                "description": "Validates system health, uptime, and availability metrics.",
                "kql": "AppAvailabilityResults\n| summarize AvailabilityPercentage = avg(toint(Success)) * 100 by bin(TimeGenerated, 1h)\n| render timechart"
            }
        ]
    }


class AdminDecisionPayload(BaseModel):
    decision: str  # APPROVED, REJECTED, AMENDMENT_REQUESTED
    notes: Optional[str] = ""
    reviewer: Optional[str] = "admin@compliance.ai"


@app.get("/api/admin/flagged-runs")
async def api_get_flagged_runs():
    """Return all non-PASS runs (RISK, FAIL, FAILED) for the admin decision desk."""
    runs = get_flagged_runs()
    return {"flagged_runs": runs}


@app.get("/api/admin/stats")
async def api_get_admin_stats():
    """Return metrics for the admin decision desk."""
    return get_admin_stats()


@app.post("/api/admin/runs/{run_id}/decision")
async def api_set_admin_decision(run_id: str, payload: AdminDecisionPayload):
    """Set compliance officer / admin decision on a flagged contract run."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    success = set_admin_decision(
        run_id=run_id,
        decision=payload.decision.upper(),
        notes=payload.notes or "",
        reviewer=payload.reviewer or "admin@compliance.ai"
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to record decision")
    
    updated_run = get_run(run_id)
    return {"status": "success", "run": updated_run}


@app.get("/api/runs/{run_id}/risk-breakdown")
async def api_get_risk_breakdown(run_id: str):
    """Return structured risk score calculation and details."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    # Load policy requirements to show "what was expected"
    policy_file = run.get("policy_file") or "policies/policies.json"
    p_path = Path(policy_file)
    if not p_path.is_file():
        if (POLICIES_DIR / p_path.name).is_file():
            p_path = POLICIES_DIR / p_path.name
        elif (ROOT_DIR / policy_file).is_file():
            p_path = ROOT_DIR / policy_file

    policies_map = {}
    if p_path.is_file():
        try:
            with open(p_path, "r", encoding="utf-8") as pf:
                p_list = json.load(pf)
                for p in p_list:
                    policies_map[p.get("policy_id")] = p
        except Exception:
            pass

    weights = {"CRITICAL": 40, "HIGH": 25, "MEDIUM": 10, "LOW": 5}
    multipliers = {"NON_COMPLIANT": 1.0, "NOT_FOUND": 1.0, "PARTIAL": 0.5, "COMPLIANT": 0.0}
    
    contributors = []
    total_score = 0.0
    
    for f in run.get("findings", []):
        status = f.get("status", "COMPLIANT")
        severity = f.get("severity", "LOW")
        weight = weights.get(severity, 5)
        multiplier = multipliers.get(status, 0.0)
        penalty = weight * multiplier
        
        formula = f"{severity} ({weight} pts) * {status.replace('_', ' ')} ({int(multiplier * 100)}%)"
        p_def = policies_map.get(f.get("policy_id"), {})
        requirement = p_def.get("requirement", "No standard requirement defined.")
        
        contributors.append({
            "policy_id": f.get("policy_id"),
            "policy_name": f.get("policy_name"),
            "status": status,
            "severity": severity,
            "penalty": penalty,
            "formula": formula,
            "expected": requirement,
            "actual_evidence": f.get("evidence") or "No direct clause reference or evidence snippet found in the contract text.",
            "status_reason": f.get("finding", "")
        })
        total_score += penalty
        
    contributors = sorted(contributors, key=lambda x: x["penalty"], reverse=True)
    capped = min(100, int(round(total_score)))
    non_compliant_count = sum(1 for c in contributors if c["penalty"] > 0)
    
    if capped == 0:
        score_explanation = "This contract is fully compliant with all checked policies. 0 risk points were accumulated."
    else:
        score_explanation = (
            f"This contract has a risk score of {capped}/100. "
            f"It accumulated a raw penalty of {total_score:.1f} points across {non_compliant_count} compliance finding(s). "
            f"Critical violations add 40 pts, High add 25 pts, Medium add 10 pts, and Low add 5 pts, multiplied by severity status."
        )
        
    return {
        "score": capped,
        "risk_level": run.get("risk_level", "LOW"),
        "score_explanation": score_explanation,
        "contributors": contributors
    }


@app.get("/api/runs/{run_id}/audit")
async def api_get_audit(run_id: str):
    """Return detailed audit trail JSON for a specific run."""
    path = f"outputs/audit/{run_id}_audit.json"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audit trail not found")
    return FileResponse(path, media_type="application/json")


@app.post("/api/run")
async def api_submit_run(
    files: List[UploadFile] = File(...),
    policy_source: str = Form(...),        # "server" or "upload"
    policy_filename: Optional[str] = Form(None),   # for server policy
    policy_file: Optional[UploadFile] = File(None), # for uploaded policy
    concurrency: Optional[int] = Form(None),
):
    """
    Accept one or more contract documents, run the compliance workflow,
    persist results to SQLite, and return all run_ids.
    """
    # Resolve policy file path
    if policy_source == "server":
        if not policy_filename:
            raise HTTPException(status_code=400, detail="policy_filename required when policy_source=server")
        if (POLICIES_DIR / policy_filename).is_file():
            policy_path = str(POLICIES_DIR / policy_filename)
        elif Path(f"policies/{policy_filename}").is_file():
            policy_path = f"policies/{policy_filename}"
        elif (ROOT_DIR / policy_filename).is_file():
            policy_path = str(ROOT_DIR / policy_filename)
        else:
            raise HTTPException(status_code=400, detail=f"Policy file not found: {policy_filename}")
    elif policy_source == "upload":
        if not policy_file:
            raise HTTPException(status_code=400, detail="policy_file upload required when policy_source=upload")
        policy_filename = Path(policy_file.filename).name if policy_file.filename else "policy.json"
        policy_path = f"uploads/{uuid.uuid4()}_{policy_filename}"
        with open(policy_path, "wb") as f:
            shutil.copyfileobj(policy_file.file, f)
    else:
        raise HTTPException(status_code=400, detail="policy_source must be 'server' or 'upload'")

    # Save uploaded contract files to temp storage
    saved_files = []
    for uf in files:
        # Extract only the base filename to prevent errors when uploading folder structures
        filename = Path(uf.filename).name if uf.filename else "document"
        dest = f"uploads/{uuid.uuid4()}_{filename}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(uf.file, f)
        saved_files.append((dest, filename))

    # Determine concurrency
    import os as _os
    auto_concurrency = concurrency if concurrency else min(_os.cpu_count() or 4, len(saved_files))
    sem = asyncio.Semaphore(min(auto_concurrency, len(saved_files)))

    workflow = get_compliance_workflow(policy_file_path=policy_path)

    async def process(file_path: str, orig_name: str, run_id: str):
        async def progress_cb(stage: str, status: str):
            await manager.send(run_id, {"run_id": run_id, "stage": stage, "status": status})

        async with sem:
            try:
                result = await workflow.execute(
                    file_path=file_path,
                    run_id=run_id,
                    progress_callback=progress_cb,
                )
                update_run_from_result(run_id, result, policy_path)
                await manager.send(run_id, {"run_id": run_id, "stage": "workflow", "status": "COMPLETED"})
            except Exception as e:
                logger.error(f"Workflow execution failed for {run_id}: {e}", exc_info=True)
                mark_run_failed(run_id, str(e))
                await manager.send(run_id, {"run_id": run_id, "stage": "workflow", "status": "FAILED", "error": str(e)})

    # Pre-generate run_ids and save initial PROCESSING records so client receives run_ids immediately
    run_ids = []
    jobs = []
    for fp, name in saved_files:
        run_id = str(uuid.uuid4())
        run_ids.append(run_id)
        create_run(run_id, name, fp, policy_path)
        jobs.append((fp, name, run_id))

    # Run processing concurrently in background
    async def run_batch():
        await asyncio.gather(*[process(fp, name, rid) for fp, name, rid in jobs])

    asyncio.create_task(run_batch())

    return {"run_ids": run_ids, "total": len(run_ids)}


@app.websocket("/api/ws/{run_id}")
async def websocket_endpoint(websocket: WebSocket, run_id: str):
    await manager.connect(run_id, websocket)
    try:
        while True:
            await websocket.receive_text()   # keep alive
    except WebSocketDisconnect:
        manager.disconnect(run_id, websocket)
    except Exception:
        manager.disconnect(run_id, websocket)
