"""Domain coverage gate.

The failure this exists to catch: a requirement about libraries produced a
task manager, every test passed, and the run reported ``accepted`` — because
the template suite tested the template app. Nothing compared the output to the
input.

The gate extracts the subject matter from the requirement and requires the
generated code to actually reflect it.

Two design points keep it from being uselessly strict:

* Terms are grouped by spelling variant (``book``/``books``), and a group is
  satisfied when *any* variant appears. Requiring every spelling would fail a
  perfectly good implementation.
* The verdict is a ratio, not "every term present". Verbs and prose words
  ("lends", "unknown") legitimately never appear in code, so demanding them
  would block correct work.
"""

from __future__ import annotations

import re

# Words describing the shape of an API, or ordinary English filler. Kept
# deliberately generous: a term wrongly treated as domain vocabulary produces
# false failures, which is the expensive direction for this gate.
GENERIC = frozenset(
    [
        "api",
        "rest",
        "apis",
        "application",
        "app",
        "service",
        "server",
        "endpoint",
        "endpoints",
        "route",
        "routes",
        "path",
        "paths",
        "http",
        "https",
        "json",
        "yaml",
        "url",
        "urls",
        "request",
        "requests",
        "response",
        "responses",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "list",
        "create",
        "read",
        "update",
        "write",
        "data",
        "model",
        "models",
        "schema",
        "schemas",
        "field",
        "fields",
        "table",
        "tables",
        "database",
        "db",
        "sqlite",
        "storage",
        "store",
        "record",
        "records",
        "entity",
        "entities",
        "restful",
        "backend",
        "frontend",
        "website",
        "web",
        "site",
        "page",
        "pages",
        "user",
        "users",
        "system",
        "systems",
        "tool",
        "tools",
        "python",
        "fastapi",
        "code",
        "project",
        "basic",
        "simple",
        "full",
        "complete",
        "version",
        "id",
        "ids",
        "name",
        "names",
        "type",
        "types",
        "value",
        "values",
        "item",
        "items",
        "entry",
        "entries",
        "object",
        "objects",
        "status",
        "error",
        "errors",
        "success",
        "message",
        "total",
        "count",
        "number",
        "order",
        "filter",
        "sort",
        "search",
        "query",
        "param",
        "params",
        "parameter",
        "parameters",
        "thing",
        "things",
        "stuff",
        "item",
        "build",
        "building",
        "create",
        "creating",
        "make",
        "making",
        "add",
        "adding",
        "use",
        "using",
        "used",
        "utilize",
        "for",
        "with",
        "that",
        "this",
        "these",
        "those",
        "from",
        "into",
        "onto",
        "each",
        "every",
        "all",
        "any",
        "some",
        "and",
        "or",
        "but",
        "not",
        "no",
        "if",
        "then",
        "than",
        "so",
        "such",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
        "also",
        "very",
        "really",
        "just",
        "only",
        "more",
        "most",
        "much",
        "many",
        "few",
        "less",
        "least",
        "be",
        "been",
        "being",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "need",
        "needs",
        "it",
        "its",
        "they",
        "them",
        "their",
        "there",
        "here",
        "now",
        "new",
        "old",
        "first",
        "last",
        "next",
        "api-style",
        "based",
        "allow",
        "allows",
        "allowed",
        "support",
        "supports",
        "supported",
        "return",
        "returns",
        "returning",
        "returned",
        "lend",
        "lends",
        "lending",
        "borrow",
        "borrows",
        "borrowing",
        "unknown",
        "missing",
        "invalid",
        "valid",
        "empty",
        "null",
        "none",
        "default",
        "defaults",
        "manage",
        "management",
        "manager",
        "managing",
        "handle",
        "handles",
        "handling",
        "keep",
        "keeps",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "by",
        "as",
        "is",
        "are",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
        "him",
        "her",
        "his",
        "hers",
        "i",
        "me",
        "my",
        "mine",
        "register",
        "registers",
        "registering",
        "registered",
        "adjust",
        "adjusts",
        "adjusting",
        "adjusted",
        "track",
        "tracks",
        "tracking",
        "tracked",
        "store",
        "stores",
        "storing",
        "stored",
        "record",
        "recording",
        "list",
        "lists",
        "listing",
        "add",
        "adds",
        "adding",
        "remove",
        "removes",
        "removing",
        "update",
        "updates",
        "check",
        "checks",
        "checking",
        "verify",
        "verifies",
        "create",
        "creates",
        "creating",
        "delete",
        "deletes",
        "calculate",
        "calculates",
        "computing",
        "compute",
        "computes",
        "total",
        "totals",
        "sum",
        "level",
        "levels",
        "amount",
        "amounts",
        "count",
        "counts",
        "quantity",
        "quantities",
        "number",
        "numbers",
        "negative",
        "positive",
        "positive-only",
        "low",
        "high",
        "available",
        "availability",
        "when",
        "where",
        "while",
        "after",
        "before",
        "during",
        "since",
        "until",
        "between",
        "among",
        "across",
        "like",
        "likes",
        "want",
        "wants",
        "need",
        "needs",
        "must",
        "should",
        "can",
        "may",
        "will",
        "shall",
        "example",
        "examples",
        "e.g",
        "etc",
        "via",
        "using",
        "use",
        "used",
        "utilize",
    ]
)

# Grouped synonyms: if any member appears, the term counts as covered. Keeps
# "book"/"books" from both needing to be present.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Minimum distinct term groups before the gate is trusted. Below this the
# requirement was too vague to judge and the gate reports inconclusive rather
# than blocking a legitimate run.
MIN_GROUPS = 2

# The verdict is deliberately an *absolute* overlap requirement, not "every
# term present". Prose and verbs from the prompt ("register", "adjust",
# "negative") legitimately never appear in code, so demanding them blocks
# correct work — which happened with a valid warehouse API at 0.58.
#
# The signal that matters is that a generation answering a different question
# shares essentially *no* vocabulary with the requirement (0 covered). A correct
# implementation always reflects the main entities.
PASS_RATIO = 0.35
MIN_COVERED = 2


def _required(n_groups: int) -> int:
    """How many groups must be reflected for a requirement of this size."""
    import math

    return max(MIN_COVERED, math.ceil(n_groups * PASS_RATIO))


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def term_groups(requirement: str) -> list[list[str]]:
    """Subject-matter groups, each a list of interchangeable spellings."""
    if not requirement:
        return []
    groups: list[list[str]] = []
    seen: set[str] = set()
    for raw in _WORD.findall(requirement):
        w = raw.lower()
        if w in GENERIC or len(w) < 3:
            continue
        variants = [w] if _singular(w) == w else [w, _singular(w)]
        key = tuple(sorted(variants))
        if key in seen:
            continue
        seen.add(key)
        groups.append(list(variants))
    return groups


def domain_terms(requirement: str) -> list[str]:
    """Flattened term list, for recording on the spec/state."""
    return [v for group in term_groups(requirement) for v in group]


def _group_mentioned(group: list[str], blob: str) -> bool:
    return any(re.search(rf"\b{re.escape(v)}\w*", blob) for v in group)


class Verdict:
    __slots__ = ("conclusive", "covered", "missing", "ratio", "total")

    def __init__(
        self, covered: list[str], missing: list[str], conclusive: bool, ratio: float, total: int
    ):
        self.covered, self.missing, self.conclusive, self.ratio = (
            covered,
            missing,
            conclusive,
            ratio,
        )
        self.total = total

    @property
    def required(self) -> int:
        return _required(self.total)

    @property
    def ok(self) -> bool:
        if not self.conclusive:
            return True
        return len(self.covered) >= self.required


def check(
    requirement: str,
    files: dict[str, str],
    routes: set[tuple[str, str]] | None = None,
) -> Verdict:
    """Decide whether ``files`` implements the requirement's subject matter."""
    groups = term_groups(requirement)
    if len(groups) < MIN_GROUPS:
        return Verdict([g[0] for g in groups], [], False, 1.0, len(groups))
    blob = "\n".join(files.values()).lower()
    for path, _method in routes or ():
        blob += "\n" + path.lower()
    covered = [g[0] for g in groups if _group_mentioned(g, blob)]
    missing = [g[0] for g in groups if not _group_mentioned(g, blob)]
    ratio = len(covered) / len(groups)
    return Verdict(covered, missing, True, round(ratio, 3), len(groups))
