"""Static inspection of generated code.

The pipeline used to assume the entrypoint was ``main.py`` holding ``app``. A
real generation names it whatever suits the design — the local model produced
``app.py`` for a library API — so every consumer of the ASGI app now resolves it
from the code itself.

Uses AST rather than grep so a *commented-out* ``app = FastAPI()`` or a string
mentioning it cannot be mistaken for the real application object.
"""

from __future__ import annotations

import ast
import re

# Attributes that can hold a FastAPI/ASGI instance. FastAPI is the default
# target, but the attribute name is the model's choice.
_APP_FACTORIES = ("FastAPI", "APIRouter")


def _module_ast(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def find_app_object(files: dict[str, str]) -> tuple[str, str] | None:
    """Return ``(module, attribute)`` for the ASGI app, or None.

    Prefers an instance of ``FastAPI(...)`` over other module-level objects,
    because a generated module often also builds engine/session objects that
    would otherwise win on name alone.
    """
    candidates: list[tuple[int, str, str]] = []
    for path, body in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        tree = _module_ast(body)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                # app = FastAPI(...)  ->  call whose func name is FastAPI
                is_app = (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in _APP_FACTORIES
                )
                if is_app:
                    candidates.append((0, path, target.id))
                elif target.id in ("app", "application", "api"):
                    # app = create_app() or app = api (an APIRouter is a router,
                    # not a runnable app, so this is only a weak match).
                    candidates.append((1, path, target.id))
    if not candidates:
        return None
    candidates.sort()
    _, path, attr = candidates[0]
    # A module inside a directory must be reported as a dotted path
    # (``routers.loans``), not ``routers/loans``, or the runner's import fails.
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", "."), attr


_ROUTE_DECORATORS = ("get", "post", "put", "patch", "delete", "head", "options", "api_route")


def routes_of(source: str) -> set[tuple[str, str]]:
    """Return ``{(path, method)}`` for every route decorator in a module.

    ``api_route`` is handled too: it carries an explicit ``methods=[...]`` list,
    so all of its methods are recorded.
    """
    tree = _module_ast(source)
    if tree is None:
        return set()
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            # Walk outward-in: ``app.router.get`` -> ['get', 'router', 'app'].
            parts: list[str] = []
            cur: ast.expr = target
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            if not parts or parts[0] not in _ROUTE_DECORATORS:
                continue
            methods = [parts[0].upper()]
            route = None
            if isinstance(dec, ast.Call):
                methods_kw = next((kw.value for kw in dec.keywords if kw.arg == "methods"), None)
                if isinstance(methods_kw, (ast.List, ast.Tuple)):
                    names = [
                        e.value
                        for e in methods_kw.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    if names:
                        methods = [n.upper() for n in names]
                if (
                    dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    route = dec.args[0].value
            if route:
                for m in methods:
                    found.add((route, m))
    return found


def all_routes(files: dict[str, str]) -> set[tuple[str, str]]:
    """Routes across every module, excluding FastAPI's own documentation paths."""
    skip = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    out: set[tuple[str, str]] = set()
    for body in files.values():
        if not body:
            continue
        for route, method in routes_of(body):
            if route not in skip:
                out.add((route, method))
    return out


def normalise_path(path: str) -> str:
    """Collapse path-parameter naming so ``/books/{isbn}`` == ``/books/{id}``.

    Two specs describing the same resource frequently disagree on the parameter
    name; without this, spec-vs-code conformance reports spurious mismatches.
    """
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")


def spec_paths(openapi_yaml: str) -> tuple[set[tuple[str, str]], set[str]]:
    """Return ``({(path, method)}, {paths})`` declared by an OpenAPI document."""
    try:
        import yaml

        doc = yaml.safe_load(openapi_yaml)
    except Exception:
        return set(), set()
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        return set(), set()
    pairs: set[tuple[str, str]] = set()
    for path, ops in doc["paths"].items():
        if not isinstance(ops, dict):
            continue
        for method in ops:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head"):
                pairs.add((path, method.upper()))
    return pairs, set(doc["paths"])


def declared_status_codes(openapi_yaml: str) -> set[str]:
    """Status codes the document declares anywhere.

    Nesting is ``paths -> <method> -> responses -> <code>``, so the codes sit two
    levels below each path; reading the keys of ``paths`` directly yields method
    names and reports nothing.
    """
    try:
        import yaml

        doc = yaml.safe_load(openapi_yaml)
    except Exception:
        return set()
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        return set()
    found: set[str] = set()
    for ops in doc["paths"].values():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if str(method).lower() not in ("get", "post", "put", "patch", "delete", "head"):
                continue
            if not isinstance(op, dict):
                continue
            responses = op.get("responses")
            if isinstance(responses, dict):
                found |= {str(c) for c in responses if str(c).isdigit()}
    return found


def uncovered_spec_endpoints(spec_yaml: str, files: dict[str, str]) -> list[tuple[str, str]]:
    """Spec endpoints with no matching route in the generated code."""
    spec_pairs, _ = spec_paths(spec_yaml)
    code = {normalise_path(p) for p, _ in all_routes(files)}
    missing: list[tuple[str, str]] = []
    for path, method in sorted(spec_pairs):
        if normalise_path(path) not in code:
            missing.append((path, method))
    return missing
