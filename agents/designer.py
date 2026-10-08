"""Designer: stories -> OpenAPI YAML. Validates, regenerates <=3 (MetaGPT SOP)."""
from __future__ import annotations
from schemas.messages import UserStories, ApiSpec
from agents.llm import generate

TEMPLATE_SPEC = """openapi: 3.1.0
info:
  title: Task Manager API
  version: 1.0.0
paths:
  /health:
    get:
      summary: Health check
      responses:
        '200':
          description: OK
  /tasks:
    get:
      summary: List tasks
      responses:
        '200':
          description: List
    post:
      summary: Create task
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/TaskCreate'
      responses:
        '201':
          description: Created
        '422':
          description: Validation error
  /tasks/{task_id}:
    get:
      summary: Get task
      parameters:
        - name: task_id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: Task
        '404':
          description: Not found
    put:
      summary: Update task
      parameters:
        - name: task_id
          in: path
          required: true
          schema:
            type: integer
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/TaskCreate'
      responses:
        '200':
          description: Updated
        '404':
          description: Not found
    delete:
      summary: Delete task
      parameters:
        - name: task_id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: Deleted
        '404':
          description: Not found
components:
  schemas:
    TaskCreate:
      type: object
      required: [title]
      properties:
        title:
          type: string
          minLength: 1
        description:
          type: string
          default: ''
        done:
          type: boolean
          default: false
        due:
          type: string
    Task:
      allOf:
        - $ref: '#/components/schemas/TaskCreate'
        - type: object
          properties:
            id:
              type: integer
"""

SOP = ("You are the API Designer. Output ONLY an OpenAPI 3.1 YAML for a task-manager "
       "CRUD API with /health and /tasks + /tasks/{id}. No prose.")


def _valid(yaml_text: str) -> tuple[bool, str]:
    try:
        import yaml
        doc = yaml.safe_load(yaml_text)
    except Exception as e:
        return False, f"yaml: {e}"
    if not isinstance(doc, dict) or "paths" not in doc or "/tasks" not in doc["paths"]:
        return False, "missing /tasks path"
    try:
        from openapi_spec_validator import validate
        validate(doc)
    except ImportError:
        pass
    except Exception as e:
        return False, f"spec: {e}"
    return True, ""


def stories_to_spec(stories: UserStories) -> ApiSpec:
    listing = "\n".join(f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories)
    for _ in range(3):
        text, meta = generate(f"Stories:\n{listing}\nOutput YAML only.", SOP, role="designer")
        cand = text.strip()
        if cand and ("openapi" in cand and "/tasks" in cand):
            ok, _ = _valid(cand)
            if ok:
                return ApiSpec(openapi_yaml=cand, endpoints=["/health", "/tasks", "/tasks/{id}"], models=["Task"])
    return ApiSpec(openapi_yaml=TEMPLATE_SPEC, endpoints=["/health", "/tasks", "/tasks/{id}"], models=["Task"])
