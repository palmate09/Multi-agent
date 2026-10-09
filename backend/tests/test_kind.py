"""Project-kind detection.

The pipeline was REST-only, so "build a three player ludo game" produced an
OpenAPI document with POST /game/roll and tests that drove it over HTTP. These
pin the classification that fixes that.
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


def test_empty_requirement_defaults_to_api():
    assert detect_kind("") == "api"


def test_plain_crud_still_classifies_as_api():
    """The original purpose of the pipeline must not regress."""
    assert detect_kind("Build a REST API for managing tasks") == "api"
