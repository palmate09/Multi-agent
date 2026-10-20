"""Project-kind detection.

The pipeline was REST-only, so "build a three player ludo game" produced an
OpenAPI document with POST /game/roll and tests that drove it over HTTP.
Then the opposite mistake took over: a plain "build me a counter" produced a
bag of JSON endpoints with nothing a person could open. These pin the
classification that fixes both.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.kind import detect_kind


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a three player ludo game in python",
        "build three player ludo game",
        "Implement a snake game with arrow keys",
        "Create a sudoku solver in python",
        "Write a binary search algorithm as a python library",
        "Make a text-based adventure game",
        "Write a Monte Carlo simulation of a dice game",
        "Build a chess engine that plays itself",
        "Create a calculator program in python",
        "Implement a neural network from scratch in numpy",
    ],
)
def test_games_and_programs_are_not_forced_into_http(requirement):
    assert detect_kind(requirement) == "program", requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a REST API for managing tasks with SQLite persistence",
        "Build a task manager REST API with CRUD /tasks plus GET /health",
        "Build an inventory REST API with POST /products",
        "Create a backend service that stores orders in a database",
        "Build an HTTP API with GET /users and POST /users returning 201",
    ],
)
def test_services_stay_services(requirement):
    assert detect_kind(requirement) == "api", requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a ludo game with a REST API to move pieces",
        "Create a REST API that simulates a ludo game board",
        "Build a web service that serves a chess game over HTTP",
        "Build a game backend with REST endpoints for moves",
    ],
)
def test_game_plus_service_is_a_service(requirement):
    """The service ask must win over the game noun, or the API is dropped."""
    assert detect_kind(requirement) == "api", requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a counter",
        "I want a task tracker where I can tick things off",
        "Make a recipe app where I can search meals by ingredient",
        "Something to keep track of my running sessions",
    ],
)
def test_a_plain_ask_becomes_a_full_stack_app(requirement):
    """The default deliverable is an app a person can open.

    Nothing here names a technology. Reading it as "a bag of JSON endpoints"
    is how users ended up with products they could not use.
    """
    assert detect_kind(requirement) == "fullstack", requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a task tracker with a dashboard",
        "Create a recipe website I can browse",
        "Build a bookstore app with a web UI",
        "Make an expense tracker, front-end included",
        "I want a single page application for taking notes",
    ],
)
def test_an_explicit_web_ask_is_a_full_stack_app(requirement):
    assert detect_kind(requirement) == "fullstack", requirement


def test_backend_only_stays_a_service():
    assert detect_kind("Build a REST API for tasks, backend only, no frontend") == "api"


@pytest.mark.parametrize(
    "requirement",
    [
        "A habit tracker where I can add daily habits, tick them off, and see a streak count.",
        "A todo planner where I can add tasks and mark them done",
        "A family tree where I can add relatives and browse the tree",
        "A reading list that keeps track of what I am reading",
    ],
)
def test_an_app_named_after_a_program_word_is_still_an_app(requirement):
    """ "planner", "tree" and "engine" are program nouns, not requests for one.

    A person asking for a todo planner wants a todo planner, not a Python
    program that prints one.
    """
    assert detect_kind(requirement) == "fullstack", requirement


def test_empty_requirement_defaults_to_fullstack():
    assert detect_kind("") == "fullstack"


def test_plain_crud_still_classifies_as_api():
    """The original purpose of the pipeline must not regress."""
    assert detect_kind("Build a REST API for managing tasks") == "api"


@pytest.mark.parametrize(
    "requirement",
    [
        "Build a three player ludo game in python",
        "make the tic tac toe game",
        "Implement a snake game with arrow keys",
        "a fibonacci calculator",
    ],
)
def test_want_ui_is_an_explicit_deliverable_vote(requirement):
    """Ticking "build a web app" makes the ask servable: backend plus a page."""
    assert detect_kind(requirement, want_ui=True) == "fullstack"
    # Without the option the same wording still classifies as a program.
    assert detect_kind(requirement) == "program"


def test_want_ui_promotes_a_plain_api_ask_to_a_full_stack_app():
    assert detect_kind("Build a REST API for tasks", want_ui=True) == "fullstack"
    assert detect_kind("Build a REST API for tasks") == "api"


def test_word_boundaries_stop_ui_matching_build():
    """ "ui" is a substring of "build"; substring matching classified
    everything as a web app."""
    assert detect_kind("build a counter") == "fullstack"
    assert detect_kind("a quick build of a price calculator") == "program"
