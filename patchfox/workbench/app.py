"""Loopback-only HTTP API and a dependency-free browser UI."""

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .artifacts import read_text, repository_head
from .demo import DEMO_PROMPT
from .service import WorkbenchService


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    max_steps: int = Field(default=30, ge=1, le=100)

    @field_validator("title", "prompt")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value


def create_app(settings):
    service = WorkbenchService(settings)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(title="PatchFox Workbench", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != expected:
            return JSONResponse({"detail": "拒绝跨来源请求"}, status_code=403)
        if (
            request.method == "POST"
            and request.headers.get("content-type", "").split(";")[0]
            != "application/json"
        ):
            return JSONResponse({"detail": "请使用 application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    @app.get("/api/config")
    def config():
        return {
            "mode": "demo" if settings.demo else "live",
            "repositories": [
                {"id": index, "name": path.name, "path": str(path)}
                for index, path in enumerate(settings.repositories)
            ],
            "test_argv": settings.test_argv,
            "demo_prompt": DEMO_PROMPT if settings.demo else "",
        }

    @app.post("/api/tasks", status_code=202)
    def submit(payload: TaskInput):
        if payload.repository_id >= len(settings.repositories):
            raise HTTPException(400, "仓库未注册，请使用启动参数 --repo 添加。")
        repo = settings.repositories[payload.repository_id]
        try:
            commit = repository_head(repo)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "无法读取 Git 仓库，请检查路径和 Git 安装。"
            )
            raise HTTPException(409, message) from exc
        return service.store.create(
            title=payload.title,
            prompt=payload.prompt,
            repository=repo,
            base_commit=commit,
            max_steps=payload.max_steps,
            test_argv=settings.test_argv,
            mode="demo" if settings.demo else "live",
            provider=settings.provider,
            model=settings.model,
        )

    @app.get("/api/tasks")
    def tasks():
        return service.store.list()

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str):
        result = service.detail(task_id)
        if result is None:
            raise HTTPException(404, "任务不存在")
        return result

    @app.get("/api/tasks/{task_id}/artifacts/{name}", response_class=PlainTextResponse)
    def artifact(task_id: str, name: str):
        if service.store.get(task_id) is None:
            raise HTTPException(404, "任务不存在")
        files = {
            "diff": "patch.diff",
            "agent": "agent.log",
            "before": "tests-before.log",
            "after": "tests-after.log",
            "setup": "setup.log",
        }
        if name not in files:
            raise HTTPException(404, "产物不存在")
        try:
            return read_text(
                service.task_dir(task_id), files[name], tail=name != "diff"
            )
        except ValueError as exc:
            raise HTTPException(403, "无效产物路径") from exc

    return app
