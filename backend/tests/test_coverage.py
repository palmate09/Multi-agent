"""The domain coverage gate.

Regression coverage for the original defect: a library requirement produced a
task manager, everything "passed", and the run reported ``accepted``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.coverage import MIN_GROUPS, check, term_groups

LIBRARY = (
    "Build a REST API for a library that lends books. POST /loans to borrow an "
    "ISBN, POST /returns to return, GET /books. 404 on unknown ISBN."
)

TASK_MANAGER = """
from fastapi import FastAPI
from sqlalchemy import Column, Integer, String
app = FastAPI()

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String)

@app.post("/tasks")
def create():
    return {}
"""

LIBRARY_APP = """
from fastapi import FastAPI
from sqlalchemy import Column, Integer, String
app = FastAPI()

class Book(Base):
    __tablename__ = "books"
    isbn = Column(String)

class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)

@app.post("/loans")
def borrow(isbn: str):
    return {}

@app.get("/books")
def books():
    return []
"""


def test_generic_api_words_are_not_domain_terms():
    terms = [g[0] for g in term_groups(LIBRARY)]
    for word in ("api", "rest", "endpoint", "http", "json", "crud"):
        assert word not in terms, word


def test_domain_subjects_are_extracted():
    terms = [g[0] for g in term_groups(LIBRARY)]
    assert "books" in terms
    assert "loans" in terms
    assert "isbn" in terms


def test_plural_and_singular_are_one_group():
    """book/books must not both be required."""
    groups = term_groups("Build a book catalogue")
    assert len(groups) == 2  # book + catalogue
    assert "book" in groups[0]


def test_correct_library_code_passes():
    verdict = check(LIBRARY, {"app.py": LIBRARY_APP}, {("/loans", "POST"), ("/books", "GET")})
    assert verdict.ok, verdict.missing
    assert verdict.ratio >= 0.6


def test_task_manager_code_fails_a_library_requirement():
    """The exact defect this gate exists to catch."""
    verdict = check(LIBRARY, {"main.py": TASK_MANAGER}, {("/tasks", "POST")})
    assert not verdict.ok
    assert verdict.ratio == 0.0
    assert "books" in verdict.missing
    assert "isbn" in verdict.missing


def test_domain_may_be_evidenced_by_routes_alone():
    verdict = check(LIBRARY, {"app.py": "app=FastAPI()\n"}, {("/loans", "POST"), ("/books", "GET")})
    assert "loans" in verdict.covered


def test_vague_requirement_is_inconclusive_not_failing():
    """Too few terms to judge: advisory, never blocks a legitimate run."""
    verdict = check("Build an API", {"a.py": "x = 1"}, set())
    assert verdict.conclusive is False
    assert verdict.ok


def test_empty_requirement_is_inconclusive():
    assert check("", {}, set()).conclusive is False


def test_min_groups_threshold():
    groups = term_groups("library books")
    assert len(groups) >= MIN_GROUPS


# --- regressions from real runs -------------------------------------------
def test_prose_and_verbs_do_not_block_a_correct_api():
    """A valid warehouse API was rejected at 0.58 because verbs were required.

    "register", "adjust", "levels" and "negative" are prose: no correct
    implementation puts them in identifiers, so they must not gate the run.
    """
    req = (
        "Build a REST API for a warehouse inventory system. Endpoints: GET /health, "
        "POST /products to register a product, GET /products to list stock levels, "
        "PUT /products/{sku} to adjust quantity. 404 when the SKU is unknown, "
        "422 when quantity is negative."
    )
    code = {
        "main.py": (
            "from fastapi import FastAPI\n"
            "from sqlalchemy import Column, Integer, String\n"
            "app = FastAPI()\n"
            "class Product(Base):\n"
            "    sku = Column(String)\n"
            "    quantity = Column(Integer)\n"
            '@app.get("/products")\n'
            "def list_products():\n"
            "    return []\n"
            '@app.post("/products")\n'
            "def add_product():\n"
            "    return {}\n"
        )
    }
    verdict = check(req, code, {("/products", "GET"), ("/products", "POST")})
    assert verdict.ok, (verdict.covered, verdict.missing)


def test_minimal_correct_library_app_is_not_blocked():
    """Small correct code covers few words but is still correct."""
    code = {
        "app.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "class Book(Base):\n"
            "    pass\n"
            "class Loan(Base):\n"
            "    pass\n"
            '@app.post("/loans")\n'
            "def borrow():\n"
            "    return {}\n"
        )
    }
    verdict = check(LIBRARY, code, {("/loans", "POST")})
    assert verdict.ok, (verdict.covered, verdict.missing)


def test_task_code_is_blocked_for_an_inventory_requirement():
    req = (
        "Build a REST API for a warehouse inventory system. POST /products to "
        "register a product, GET /products, PUT /products/{sku} to adjust quantity."
    )
    verdict = check(req, {"main.py": TASK_MANAGER}, {("/tasks", "POST")})
    assert not verdict.ok
    assert verdict.covered == []


def test_required_scales_with_requirement_size():
    from agents.coverage import _required

    assert _required(1) == 2
    assert _required(5) == 2
    assert _required(10) == 4
    assert _required(40) == 14
