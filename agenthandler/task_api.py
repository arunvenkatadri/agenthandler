"""Optional authenticated API for application-registered durable workflows."""

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .task import DurableTaskRunner, Milestone, TaskBusyError, TaskLimits


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    title: str
    goal: str
    milestones: Sequence[Milestone]
    limits: TaskLimits = field(default_factory=TaskLimits)
    validate_inputs: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None


class CreateTaskRequest(BaseModel):
    template_id: str = Field(..., max_length=256)
    inputs: Dict[str, Any] = Field(default_factory=dict)


def register_task_routes(
    app: FastAPI,
    runner: DurableTaskRunner,
    templates: Sequence[TaskTemplate],
    auth: Any,
) -> None:
    registry = {t.id: t for t in templates}
    if len(registry) != len(templates):
        raise ValueError("Task template IDs must be unique")

    @app.get("/task-templates")
    def list_templates(_: Any = Depends(auth)) -> Any:
        return [
            {"id": t.id, "title": t.title, "goal": t.goal, "limits": asdict(t.limits)}
            for t in templates
        ]

    @app.get("/tasks")
    def list_tasks(_: Any = Depends(auth)) -> Any:
        return [asdict(record) for record in runner.store.list_tasks()]

    @app.post("/tasks", status_code=201)
    def create_task(req: CreateTaskRequest, _: Any = Depends(auth)) -> Any:
        template = registry.get(req.template_id)
        if template is None:
            raise HTTPException(404, "Task template not registered")
        try:
            inputs = (
                template.validate_inputs(req.inputs) if template.validate_inputs else req.inputs
            )
            return asdict(
                runner.create(
                    template.id,
                    template.goal,
                    template.milestones,
                    limits=template.limits,
                    inputs=inputs,
                    template_id=template.id,
                )
            )
        except TaskBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str, _: Any = Depends(auth)) -> Any:
        try:
            return asdict(runner.store.load(task_id))
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

    @app.post("/tasks/{task_id}/run")
    async def run_task(task_id: str, _: Any = Depends(auth)) -> Any:
        try:
            record = runner.store.load(task_id)
            template = registry.get(record.template_id or "")
            if template is None:
                raise HTTPException(409, "Original task template is no longer registered")
            return asdict(await runner.run(task_id, template.milestones))
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc
        except (TaskBusyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
