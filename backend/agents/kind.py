"""What kind of project is this requirement asking for?

The pipeline was written for REST APIs, so "build a three player ludo game"
produced an OpenAPI document with ``POST /game/roll`` and tests that drove it
over HTTP. That answers a question nobody asked.

Two kinds are supported:

``api``      a service with HTTP endpoints. Contract is OpenAPI; the app object
             is found by AST inspection; tests drive it with TestClient.
``program``  a self-contained piece of software — a game, a simulation, an
             algorithm, a library. Contract is a declared public interface;
             tests import the symbols and assert behaviour directly.

Detection is a weighted keyword vote rather than a single check, because
requirements mix signals: "a ludo game with a REST API to move pieces" is an
api, while "a ludo game in python" is a program.
"""

from __future__ import annotations

import re

# Signals that the deliverable is an HTTP service.
_API_SIGNALS = (
    "rest api", "restful", "rest", "endpoint", "endpoints", "api",
    "http", "server", "backend", "service", "web service", "microservice",
    "crud", "route", "routes", "get post put delete patch",
    "database", "sqlite", "postgres", "sql", "orm", "database-backed",
    "request", "response", "json payload", "status code", "status codes",
    "openapi", "swagger", "curl", "http client",
)

# Signals that the deliverable is a self-contained program.
_PROGRAM_SIGNALS = (
    "game", "games", "play", "player", "players", "turn", "moves", "board",
    "dice", "score", "simulation", "simulate", "simulator", "algorithm",
    "algorithm(s)", "library", "package", "module", "function", "class",
    "cli", "command line", "script", "program", "programme", "puzzle",
    "solver", "calculator", "parser", "generator", "random", "probability",
    "console", "terminal", "text based", "text-based", "draw", "render",
    "animation", "snake", "chess", "ludo", "sudoku", "tetris",
    "neural network", "network from scratch", "from scratch", "machine learning",
    "machine learning model", "perceptron", "train", "training", "epoch",
    "gradient", "matrix", "vector", "graph algorithm", "tree", "graph",
    "encrypt", "encryption", "cipher", "hash", "compress",
    "scrambler", "solver", "planner", "engine", "bot", "ai", "agent",
    "implement", "write a", "create a", "make a",
)

# Words that flip an apparent game request back toward a service: the program is
# still delivered over HTTP or backed by storage.
_API_OVERRIDE = (
    "rest api", "restful", "api", "endpoint", "endpoints", "http", "server",
    "backend", "crud", "database", "sqlite", "openapi", "request", "response",
)


def _count(text: str, signals: tuple[str, ...]) -> int:
    return sum(1 for s in signals if s in text)


def detect_kind(requirement: str) -> str:
    """Return ``"api"`` or ``"program"``.

    A program signal only wins when the requirement does not also ask for a
    service. A tie resolves to ``"api"``, which preserves the original
    behaviour for the plain CRUD requests this pipeline was built for.
    """
    if not requirement:
        return "api"
    text = " " + re.sub(r"[^a-z0-9./+-]+", " ", requirement.lower()) + " "
    program = _count(text, _PROGRAM_SIGNALS)
    if program == 0:
        return "api"
    api = _count(text, _API_SIGNALS)
    # An explicit service request overrides a game/algorithm noun.
    if _count(text, _API_OVERRIDE) > 0 and api > 0:
        return "api"
    if api > program:
        return "api"
    return "program"


def kind_hint(requirement: str) -> str:
    """One-line explanation, logged so a misclassification is diagnosable."""
    kind = detect_kind(requirement)
    if kind == "api":
        return "detected: HTTP service (OpenAPI contract, TestClient tests)"
    return "detected: self-contained program (declared interface, direct logic tests)"