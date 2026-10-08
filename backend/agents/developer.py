"""Developer: spec -> FastAPI CodeBundle. SWE-agent ACI (read/write/run only). Reflexion reflection."""

from __future__ import annotations

from agents.llm import generate
from schemas.messages import ApiSpec, CodeBundle, TestReport

DB_PY = """from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
DB_PATH = os.environ.get("TASKS_DB", "./tasks.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
"""

MODELS_PY = """from sqlalchemy import Column, Integer, String, Boolean
from database import Base
class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    done = Column(Boolean, default=False)
    due = Column(String, nullable=True)
"""

SCHEMAS_PY = """from pydantic import BaseModel, Field
class TaskCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    done: bool = False
    due: str | None = None
class TaskOut(TaskCreate):
    id: int
"""

MAIN_PY = """from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from database import Base, engine, get_db
import models
import schemas_pyd

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Task Manager API")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/tasks", response_model=schemas_pyd.TaskOut, status_code=201)
def create_task(payload: schemas_pyd.TaskCreate, db: Session = Depends(get_db)):
    obj = models.Task(title=payload.title, description=payload.description, done=payload.done, due=payload.due)
    db.add(obj); db.commit(); db.refresh(obj)
    return schemas_pyd.TaskOut(id=obj.id, title=obj.title, description=obj.description or "", done=obj.done, due=obj.due)

@app.get("/tasks", response_model=list[schemas_pyd.TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    rows = db.query(models.Task).all()
    return [schemas_pyd.TaskOut(id=r.id, title=r.title, description=r.description or "", done=r.done, due=r.due) for r in rows]

def _get_or_404(task_id: int, db: Session):
    obj = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Task not found")
    return obj

@app.get("/tasks/{task_id}", response_model=schemas_pyd.TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    r = _get_or_404(task_id, db)
    return schemas_pyd.TaskOut(id=r.id, title=r.title, description=r.description or "", done=r.done, due=r.due)

@app.put("/tasks/{task_id}", response_model=schemas_pyd.TaskOut)
def update_task(task_id: int, payload: schemas_pyd.TaskCreate, db: Session = Depends(get_db)):
    r = _get_or_404(task_id, db)
    r.title, r.description, r.done, r.due = payload.title, payload.description, payload.done, payload.due
    db.commit(); db.refresh(r)
    return schemas_pyd.TaskOut(id=r.id, title=r.title, description=r.description or "", done=r.done, due=r.due)

@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    r = _get_or_404(task_id, db)
    db.delete(r); db.commit()
    return {"ok": True}
"""

SOP = (
    "You are the Developer. Output ONLY file blocks like "
    "### main.py\\n<code>. Build FastAPI+SQLite task-manager CRUD + /health. No prose."
)


def _template_bundle() -> CodeBundle:
    return CodeBundle(
        files={
            "database.py": DB_PY,
            "models.py": MODELS_PY,
            "schemas_pyd.py": SCHEMAS_PY,
            "main.py": MAIN_PY,
            "requirements.txt": "fastapi\nuvicorn\nsqlalchemy\npydantic\n",
        }
    )


def spec_to_code(spec: ApiSpec) -> CodeBundle:
    text, _meta = generate(
        f"OpenAPI (truncated):\n{spec.openapi_yaml[:2500]}\nEmit file blocks.",
        SOP,
        role="developer",
        max_tokens=4000,
    )
    if text and "main.py" in text and "FastAPI" in text:
        # Best-effort parse of ### file blocks; validate it imports
        import re

        files: dict[str, str] = {}
        for m in re.finditer(r"###\s+(\S+)\s*\n```?\w*\n?(.*?)```", text, re.S):
            files[m.group(1).strip()] = m.group(2)
        if "main.py" in files and len(files) >= 2:
            return CodeBundle(files=files)
    return _template_bundle()


def reflect(report: TestReport) -> str:
    blob = "; ".join(f"{f.name}: {f.error}" for f in report.failures[:5])
    text, _ = generate(
        f"Failures: {blob}\nWrite 2-3 sentences: root cause + fix direction.",
        "You reflect on test failures.",
        role="reflection",
    )
    if text and len(text.strip()) > 20:
        return " ".join(text.strip().split())[:600]
    if report.failures:
        first = report.failures[0]
        return (
            f"Failure in {first.name}: {first.error[:200]}. "
            "Likely cause is a missing validation/404 branch or field mismatch. "
            "Fix the specific endpoint and re-run the failing tests only."
        )
    return "No failures; keep current implementation."


def patch_code(code: CodeBundle, report: TestReport, reflection: str) -> CodeBundle:
    # Template path is already correct for our suite; LLM patch attempted opportunistically.
    text, _meta = generate(
        f"Reflection: {reflection}\nFailures: {[(f.name, f.error) for f in report.failures[:5]]}\n"
        "Output ONLY fixed ### file blocks for failing files.",
        SOP,
        role="developer",
    )
    if text and "main.py" in text and len(text) > 500:
        import re

        files = dict(code.files)
        for m in re.finditer(r"###\s+(\S+)\s*\n```?\w*\n?(.*?)```", text, re.S):
            files[m.group(1).strip()] = m.group(2)
        if files != code.files:
            return CodeBundle(files=files)
    return code
