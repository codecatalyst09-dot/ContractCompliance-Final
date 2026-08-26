import os
import sys
import glob
import asyncio
import argparse
from typing import List

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.workflow.compliance_workflow import ContractComplianceWorkflow, get_compliance_workflow
from src.monitoring.logging_config import get_logger

# Export top-level FastAPI instance for Vercel / ASGI runners
try:
    from src.api.app import app
except Exception:
    app = None

logger = get_logger("main_cli")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Contract Compliance Agent (MAF) — Automated Document Compliance & Risk Analysis",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # --- Input: exactly one of --file or --folder is required ---
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--file",
        type=str,
        help="Path to a single contract document (PDF, DOCX, TXT) to process."
    )
    input_group.add_argument(
        "--folder",
        type=str,
        help="Path to a directory containing contract documents to process."
    )

    # --- Policy file: required explicitly ---
    parser.add_argument(
        "--policy-file",
        type=str,
        required=True,
        help="Path to the policies JSON file (e.g. policies/policies.json)."
    )

    # --- Concurrency: optional, auto-detected if not set ---
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of documents to process in parallel simultaneously.\n"
             "If not set, automatically uses min(CPU count, total files found)."
    )

    # --- Optional arguments ---
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional custom run ID to correlate a single-file execution."
    )
    parser.add_argument(
        "--use-doc-intel",
        action="store_true",
        help="Enable Azure AI Document Intelligence for scanned PDF layout analysis."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose clause-level findings to console after each document."
    )

    return parser.parse_args()


def discover_documents(target_path: str) -> List[str]:
    """Finds supported contract document files (.pdf, .docx, .doc, .txt) in a path."""
    if os.path.isfile(target_path):
        return [target_path]
    elif os.path.isdir(target_path):
        allowed_exts = ("*.pdf", "*.docx", "*.doc", "*.txt")
        files: List[str] = []
        for ext in allowed_exts:
            files.extend(glob.glob(os.path.join(target_path, ext)))
            files.extend(glob.glob(os.path.join(target_path, "**", ext), recursive=True))
        return sorted(list(set(files)))
    else:
        return []


def validate_inputs(args: argparse.Namespace) -> None:
    """Validates that all provided paths and settings are usable before execution begins."""
    target = args.file or args.folder

    if args.file and not os.path.isfile(args.file):
        print(f"[ERROR] --file path does not exist or is not a file: '{args.file}'")
        sys.exit(1)

    if args.folder and not os.path.isdir(args.folder):
        print(f"[ERROR] --folder path does not exist or is not a directory: '{args.folder}'")
        sys.exit(1)

    if not os.path.isfile(args.policy_file):
        print(f"[ERROR] --policy-file not found: '{args.policy_file}'")
        sys.exit(1)

    if args.concurrency is not None and args.concurrency < 1:
        print(f"[ERROR] --concurrency must be >= 1, got: {args.concurrency}")
        sys.exit(1)


async def process_single_document(
    workflow: ContractComplianceWorkflow,
    doc_path: str,
    idx: int,
    total: int,
    semaphore: asyncio.Semaphore,
    args: argparse.Namespace,
):
    file_name = os.path.basename(doc_path)
    async with semaphore:
        print(f"[{idx}/{total}] STARTING '{file_name}'...")
        try:
            result = await workflow.execute(
                file_path=doc_path,
                run_id=args.run_id if total == 1 else None,
                use_document_intelligence=args.use_doc_intel,
            )

            is_contract = result.classification.is_contract
            doc_type = result.classification.document_type.value

            if not is_contract:
                print(
                    f"[{idx}/{total}] COMPLETED '{file_name}':\n"
                    f"  |-- [SKIPPED] Classified as '{doc_type}', not a contract.\n"
                    f"  \\-- Audit record: outputs/audit/{result.run_id}_audit.json\n"
                )
                return result

            status = result.compliance.overall_status if result.compliance else "UNKNOWN"
            score = result.risk.score if result.risk else 0
            risk_level = result.risk.risk_level.value if result.risk else "UNKNOWN"
            obl_count = len(result.obligations.obligations) if result.obligations else 0

            print(
                f"[{idx}/{total}] COMPLETED '{file_name}':\n"
                f"  |-- Document Type: {doc_type} (Confidence: {result.classification.confidence * 100:.0f}%)\n"
                f"  |-- Extracted Obligations: {obl_count}\n"
                f"  |-- Overall Compliance: [{status}]\n"
                f"  |-- Risk Score: {score}/100 ({risk_level} Risk)\n"
                f"  |-- Recommendations: {len(result.recommendations)}\n"
                f"  \\-- Artifacts Generated:\n"
                f"      * Report: outputs/compliance/{result.run_id}_report.md\n"
                f"      * JSON:   outputs/compliance/{result.run_id}_compliance.json\n"
                f"      * Audit:  outputs/audit/{result.run_id}_audit.json\n"
            )

            if args.verbose and result.compliance:
                print(f"[{idx}/{total}] Clause Findings for '{file_name}':")
                for f in result.compliance.findings:
                    print(f"    [{f.policy_id}] {f.policy_name}: {f.status.value} ({f.severity.value})")
                print()

            return result

        except Exception as e:
            print(f"[{idx}/{total}] FAILED '{file_name}':\n  \\-- Error: {e}\n")
            logger.error(f"Error processing '{file_name}'", exc_info=True)
            return None


async def main_async():
    args = parse_args()
    validate_inputs(args)

    target = args.file or args.folder
    documents = discover_documents(target)

    if not documents:
        print(f"[ERROR] No supported documents found at '{target}'. Supported formats: .pdf, .docx, .txt")
        sys.exit(1)

    import os as _os
    auto_concurrency = args.concurrency if args.concurrency is not None else min(_os.cpu_count() or 4, len(documents))
    concurrency = min(auto_concurrency, len(documents))

    print(f"\n=======================================================")
    print(f" CONTRACT COMPLIANCE AGENT (MAF Orchestration)")
    print(f" Target      : {target}")
    print(f" Policy File : {args.policy_file}")
    print(f" Documents   : {len(documents)} found")
    concurrency_label = f"{concurrency} parallel" + (" (auto)" if args.concurrency is None else "")
    print(f" Concurrency : {concurrency_label}")
    print(f"=======================================================\n")

    workflow = get_compliance_workflow(policy_file_path=args.policy_file)
    semaphore = asyncio.Semaphore(concurrency)

    tasks = [
        process_single_document(workflow, doc_path, idx, len(documents), semaphore, args)
        for idx, doc_path in enumerate(documents, start=1)
    ]

    await asyncio.gather(*tasks)

    print("All documents processed. Log entries written to logs/application.jsonl\n")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
