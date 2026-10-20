"""What kind of project is this requirement asking for?

The pipeline was written for REST APIs, so "build a three player ludo game"
produced an OpenAPI document with ``POST /game/roll`` and tests that drove it
over HTTP. That answers a question nobody asked. The opposite mistake then
became the common one: a plain "build me a counter" produced a bag of JSON
endpoints with nothing a person could actually click.

Three kinds are supported:

``fullstack``  a web application a person can use: an HTTP backend plus a real
               frontend that the application itself serves at ``/``. This is
               the default deliverable, because "build me a thing" almost always
               means "build me something I can open in a browser".
``api``        a backend service with HTTP endpoints and no user interface. The
               contract is OpenAPI; the app object is found by AST inspection;
               tests drive it with TestClient.
``program``    a self-contained piece of software — a game, a simulation, an
               algorithm, a library. Contract is a declared public interface;
               tests import the symbols and assert behaviour directly.

Detection is a weighted keyword vote rather than a single check, because
requirements mix signals: "a ludo game with a REST API to move pieces" is an
api, while "a ludo game in python" is a program, and "a task tracker" is a
full-stack app even though it names no technology at all.

Signals are matched on word boundaries. Substring matching made "build" look
like the word "ui", which would have turned every requirement into a web app.
"""

from __future__ import annotations

import re

# Signals that the deliverable is an HTTP service with no user interface.
_API_SIGNALS = (
    "rest api",
    "rest apis",
    "restful",
    "rest",
    "endpoint",
    "endpoints",
    "api",
    "apis",
    "http",
    "server",
    "backend",
    "web service",
    "microservice",
    "crud",
    "route",
    "routes",
    "openapi",
    "swagger",
    "curl",
    "http client",
    "webhook",
    "webhooks",
    "sdk",
    "client library",
    "status code",
    "status codes",
    "json payload",
    "get post put delete patch",
)

# The user asked for a backend and nothing else. These win outright: a
# full-stack app for someone who said "backend only" is a different product.
_API_ONLY_SIGNALS = (
    "api only",
    "only the api",
    "just the api",
    "backend only",
    "just the backend",
    "no frontend",
    "no web ui",
    "no ui",
    "headless",
    "backend service only",
    "library of endpoints",
)

# Signals that the deliverable is a web application a person uses.
_WEB_SIGNALS = (
    "web app",
    "web application",
    "web ui",
    "web front end",
    "web frontend",
    "web site",
    "website",
    "web page",
    "web pages",
    "web portal",
    "web dashboard",
    "front end",
    "frontend",
    "user interface",
    "graphical interface",
    "gui",
    "dashboard",
    "admin panel",
    "browser",
    "browser based",
    "browser-based",
    "spa",
    "single page application",
    "react",
    "vue",
    "svelte",
    "angular",
    "landing page",
    "storefront",
    "on the web",
    "in the browser",
    "responsive",
    "mobile friendly",
)

# Signals that the deliverable is a self-contained program.
_PROGRAM_SIGNALS = (
    "game",
    "games",
    "play",
    "player",
    "players",
    "turn",
    "moves",
    "board",
    "dice",
    "simulation",
    "simulate",
    "simulator",
    "algorithm",
    "algorithms",
    "library",
    "python library",
    "python package",
    "module",
    "cli",
    "command line",
    "script",
    "program",
    "programme",
    "puzzle",
    "solver",
    "calculator",
    "random",
    "probability",
    "console",
    "terminal",
    "text based",
    "text-based",
    "desktop app",
    "desktop application",
    "snake",
    "chess",
    "ludo",
    "sudoku",
    "tetris",
    "neural network",
    "network from scratch",
    "from scratch",
    "machine learning",
    "machine learning model",
    "perceptron",
    "train",
    "training",
    "epoch",
    "gradient",
    "encrypt",
    "encryption",
    "cipher",
    "hash",
    "compress",
    "scrambler",
    "bot",
    "chatbot",
    "ai agent",
)

# Words that flip an apparent game request back toward a service: the program is
# still delivered over HTTP or backed by storage.
_API_OVERRIDE = (
    "rest api",
    "restful",
    "api",
    "endpoint",
    "endpoints",
    "http",
    "server",
    "backend",
    "crud",
    "database",
    "sqlite",
    "openapi",
    "request",
    "response",
)


def _count(text: str, signals: tuple[str, ...]) -> int:
    """How many signals appear in the text, matched on word boundaries.

    Word boundaries matter: "ui" is a substring of "build", and "app" is a
    substring of "apple". Substring matching would have classified almost
    every requirement as a web app.
    """
    hits = 0
    for signal in signals:
        pattern = rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])"
        if re.search(pattern, text):
            hits += 1
    return hits


def _has(text: str, signals: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])", text) for signal in signals
    )


def detect_kind(requirement: str, want_ui: bool = False) -> str:
    """Return ``"fullstack"``, ``"api"`` or ``"program"``.

    The order of the rules is the policy:

    1. An explicit "backend only" wins over everything.
    2. A program (game, library, CLI) stays a program unless the requirement
       also asks for a service or a web app.
    3. An explicit web ask is a full-stack application.
    4. An explicit service ask is an API.
    5. Anything else is a full-stack application, because a bare "build me a
       counter" is a thing a person should be able to open in a browser.

    ``want_ui`` is the run's "build a web app" option. It is an explicit
    deliverable choice, so it votes like the strongest web signal there is.
    """
    if not requirement or not requirement.strip():
        return "fullstack"
    text = " " + re.sub(r"[^a-z0-9./+-]+", " ", requirement.lower()) + " "
    if _has(text, _API_ONLY_SIGNALS):
        return "api"
    program = _count(text, _PROGRAM_SIGNALS)
    api = _count(text, _API_SIGNALS)
    web = _count(text, _WEB_SIGNALS)
    if want_ui:
        web += 2
    if program and not web and not api:
        return "program"
    # An explicit web ask (or the run's web-app option) outranks the service
    # nouns: "a REST API and a web UI" is one full-stack application, not a
    # backend with a page bolted on beside it.
    if web:
        return "fullstack"
    # An explicit service request overrides a game/algorithm noun.
    if _count(text, _API_OVERRIDE) > 0 and api > 0:
        return "api"
    if api:
        return "api"
    return "fullstack"


def kind_hint(requirement: str, want_ui: bool = False) -> str:
    """One-line explanation, logged so a misclassification is diagnosable."""
    kind = detect_kind(requirement, want_ui=want_ui)
    if kind == "fullstack":
        return (
            "detected: full-stack web application (OpenAPI contract plus a "
            "frontend the app serves at /)"
        )
    if kind == "api":
        return "detected: HTTP service (OpenAPI contract, TestClient tests)"
    return "detected: self-contained program (declared interface, direct logic tests)"
