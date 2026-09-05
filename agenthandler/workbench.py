"""Runnable local reference application: authenticated, durable order reports.

Run ``python -m agenthandler.workbench --data-dir .agenthandler`` with
AGENTHANDLER_API_KEY set. This deterministic workflow makes no model calls.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .completion import VerificationResult
from .server import create_app
from .session import SessionManager
from .store import SqliteStore
from .task import (
    DurableTaskRunner,
    Milestone,
    RecoveryResult,
    SqliteTaskStore,
    TaskContext,
    TaskLimits,
)
from .task_api import TaskTemplate


def summarize_orders(inputs: Dict[str, Any]) -> Dict[str, Any]:
    text = inputs.get("orders")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 10000:
        raise ValueError("Provide an orders CSV of at most 10 KB")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != ["item", "quantity", "unit_price"]:
        raise ValueError("CSV header must be item,quantity,unit_price")
    items: list[Dict[str, Any]] = []
    for row in reader:
        if len(items) >= 100 or None in row or any(value is None for value in row.values()):
            raise ValueError("Provide at most 100 complete rows")
        try:
            quantity = int(row["quantity"])
            price = Decimal(row["unit_price"])
            if not price.is_finite() or price < 0 or price > 1000000:
                raise ValueError("Unit prices must be between 0 and 1,000,000")
            cents = price * 100
            if cents != cents.to_integral_value():
                raise ValueError("Unit prices must have at most two decimal places")
            if not row["item"].strip() or quantity < 1 or quantity > 1000000:
                raise ValueError("Each item needs a name and a positive bounded quantity")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid order row: {exc}") from exc
        items.append(
            {"item": row["item"], "quantity": quantity, "amount_cents": quantity * int(cents)}
        )
    if not items:
        raise ValueError("Provide at least one order")
    return {"items": items, "total_cents": sum(row["amount_cents"] for row in items)}


def report_template(directory: Path) -> TaskTemplate:
    artifacts = directory / "reports"
    artifacts.mkdir(parents=True, exist_ok=True)

    def validate(inputs: Dict[str, Any]) -> Dict[str, Any]:
        summarize_orders(inputs)
        return {"orders": inputs["orders"]}

    async def calculate(ctx: TaskContext) -> Any:
        return summarize_orders(ctx.inputs)

    async def check_calculation(ctx: TaskContext) -> VerificationResult:
        passed = ctx.output == summarize_orders(ctx.inputs)
        return VerificationResult(passed, {"recomputed_total_cents": ctx.output["total_cents"]})

    def expected(ctx: TaskContext) -> Dict[str, Any]:
        return {"operation_id": ctx.operation_id, "report": summarize_orders(ctx.inputs)}

    async def publish(ctx: TaskContext) -> Any:
        receipt = expected(ctx)
        target = artifacts / f"{ctx.operation_id}.json"
        if target.exists():
            raise ValueError("Report already exists; reconcile the prior operation")
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=artifacts, delete=False) as stream:
                temporary = stream.name
                json.dump(receipt, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return receipt

    async def reconcile(ctx: TaskContext) -> RecoveryResult:
        target = artifacts / f"{ctx.operation_id}.json"
        if not target.exists():
            return RecoveryResult("absent")
        try:
            receipt = json.loads(target.read_text())
        except (ValueError, OSError):
            return RecoveryResult("unknown", reason="Report receipt cannot be read")
        if receipt != expected(ctx):
            return RecoveryResult("unknown", reason="Report receipt does not match the request")
        return RecoveryResult("completed", receipt)

    async def check_report(ctx: TaskContext) -> VerificationResult:
        target = artifacts / f"{ctx.operation_id}.json"
        receipt = json.loads(target.read_text())
        return VerificationResult(
            receipt == expected(ctx) == ctx.output,
            {"artifact": target.name, "total_cents": receipt["report"]["total_cents"]},
            "Reopened the saved report and checked it against the original orders",
        )

    return TaskTemplate(
        "order-report",
        "Order report",
        "Calculate and save a verified order report",
        [
            Milestone(
                "calculate",
                "Every order is included in an exact total",
                calculate,
                check_calculation,
            ),
            Milestone(
                "publish",
                "Saved report matches the original orders",
                publish,
                check_report,
                reconcile,
            ),
        ],
        TaskLimits(max_calls=12, max_tokens=0, max_cost_microusd=0),
        validate,
    )


def create_workbench_app(directory: str, api_key: Optional[str] = None) -> FastAPI:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(SqliteStore(str(root / "sessions.db")))
    runner = DurableTaskRunner(manager, SqliteTaskStore(str(root / "tasks.db")))
    app = create_app(
        manager,
        api_key=api_key,
        require_auth=True,
        task_runner=runner,
        task_templates=[report_template(root)],
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "workbench.html")

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=".agenthandler")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not os.environ.get("AGENTHANDLER_API_KEY"):
        parser.error("Set AGENTHANDLER_API_KEY before starting the workbench")
    uvicorn.run(create_workbench_app(args.data_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
