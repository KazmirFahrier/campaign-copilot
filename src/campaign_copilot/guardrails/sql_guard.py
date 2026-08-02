"""Static SQL validation, performed on the AST, before anything reaches the warehouse.

A string-matching guard ("does the query contain the word DROP?") is defeated by
``/**/``, by ``dRoP``, by a comment, and by a second statement after a semicolon. So we
parse instead. Every rule below operates on the parsed tree.

Rules, in order:

1. The text must parse as exactly one statement.
2. That statement must be a ``SELECT`` (optionally with CTEs, optionally a set
   operation). Anything else -- ``INSERT``, ``DROP``, ``ATTACH``, ``COPY``, ``INSTALL``,
   ``PRAGMA`` -- is rejected. DuckDB's ``ATTACH`` and ``read_csv`` are the interesting
   ones: they are how a prompt-injected agent reaches the filesystem.
3. Every table referenced must be on the allowlist. CTE names are resolved first so a
   CTE cannot masquerade as a table.
4. Table functions that touch the filesystem or network are rejected by name.
5. ``SELECT *`` is rejected by default: it makes result-set diffs in the eval harness
   unstable and it leaks columns the agent was never told about.
6. Every aggregate must be an atom of a registered metric. ``avg(roas)`` is rejected;
   ``sum(revenue_usd)`` is allowed. This is what stops the agent inventing arithmetic.
7. A query must read an allowlisted table, and a projected numeric literal is rejected.
   Otherwise the model can turn its own guess into a "tool fact" with
   ``SELECT 412000 AS spend`` and defeat the grounding gate.
8. A ``LIMIT`` is injected if absent and clamped if excessive.

The guard returns rewritten SQL. Callers execute *that*, never the original string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

if TYPE_CHECKING:
    from campaign_copilot.semantic.layer import SemanticLayer

__all__ = [
    "GuardResult",
    "GuardrailViolation",
    "SqlGuard",
    "SqlGuardConfig",
    "ViolationCode",
]

DIALECT: Final = "duckdb"

#: Table functions that read the filesystem, the network, or DuckDB internals.
FORBIDDEN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_text",
        "read_blob",
        "parquet_scan",
        "csv_scan",
        "glob",
        "sniff_csv",
        "load",
        "install",
        "duckdb_settings",
        "duckdb_extensions",
        "getenv",
        "shell",
        "system",
    }
)


class ViolationCode(str):
    """Machine-readable rejection reasons, returned to the model on failure."""

    PARSE_ERROR = "PARSE_ERROR"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_A_SELECT = "NOT_A_SELECT"
    TABLE_NOT_ALLOWED = "TABLE_NOT_ALLOWED"
    FUNCTION_NOT_ALLOWED = "FUNCTION_NOT_ALLOWED"
    STAR_NOT_ALLOWED = "STAR_NOT_ALLOWED"
    UNREGISTERED_AGGREGATE = "UNREGISTERED_AGGREGATE"
    TABLE_REQUIRED = "TABLE_REQUIRED"
    LITERAL_PROJECTION = "LITERAL_PROJECTION"


class GuardrailViolation(Exception):  # noqa: N818
    """A query was rejected. ``code`` is fed back to the model; ``message`` to the log."""

    def __init__(self, code: str, message: str) -> None:
        """Record the machine-readable ``code`` and the human-readable ``message``."""
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SqlGuardConfig:
    """Policy for a single guard instance."""

    allowed_tables: frozenset[str] = frozenset(
        {
            "main_marts.campaign_performance_daily",
            "main_marts.channel_attribution",
            "main_marts.customer_ltv",
        }
    )
    allowed_aggregates: frozenset[str] = frozenset()
    default_limit: int = 100
    max_limit: int = 10_000
    allow_star: bool = False
    require_registered_aggregates: bool = True

    @property
    def allowed_bare_tables(self) -> frozenset[str]:
        """Allowlisted table names with their schema stripped."""
        return frozenset(t.split(".")[-1] for t in self.allowed_tables)


@dataclass(slots=True)
class GuardResult:
    """The rewritten, safe-to-execute query plus anything the caller should know."""

    sql: str
    warnings: list[str] = field(default_factory=list)


class SqlGuard:
    """Validates and rewrites a candidate query."""

    def __init__(self, config: SqlGuardConfig | None = None) -> None:
        """Build a guard with ``config``, or a conservative default policy."""
        self.config = config or SqlGuardConfig()

    # ------------------------------------------------------------------ public

    def check(self, sql: str) -> GuardResult:
        """Validate ``sql`` and return a rewritten, safe-to-execute query.

        Raises:
            GuardrailViolation: if any rule fails. Inspect ``.code`` to repair.
        """
        tree = self._parse_single(sql)
        self._assert_select(tree)

        warnings: list[str] = []
        # Functions first. `read_csv('/etc/passwd')` in a FROM clause parses as a Table whose
        # name is not on the allowlist, so checking tables first reported TABLE_NOT_ALLOWED and
        # FORBIDDEN_FUNCTIONS was never reached. Deleting the entire denylist broke no test
        # (docs/AUDIT.md, R2-7), because the test accepted either violation code.
        self._check_functions(tree)
        self._check_tables(tree)
        self._check_table_source(tree)
        if not self.config.allow_star:
            self._check_star(tree)
        self._check_projected_literals(tree)
        if self.config.require_registered_aggregates and self.config.allowed_aggregates:
            self._check_aggregates(tree)

        tree = self._enforce_limit(tree, warnings)
        return GuardResult(sql=tree.sql(dialect=DIALECT, pretty=True), warnings=warnings)

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _parse_single(sql: str) -> exp.Expression:
        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except ParseError as err:
            raise GuardrailViolation(
                ViolationCode.PARSE_ERROR, f"Query is not valid DuckDB SQL: {err}"
            ) from err
        if not statements:
            raise GuardrailViolation(ViolationCode.PARSE_ERROR, "Empty query.")
        if len(statements) > 1:
            raise GuardrailViolation(
                ViolationCode.MULTIPLE_STATEMENTS,
                f"Expected exactly one statement, found {len(statements)}. "
                "Statement batching is how a SELECT smuggles a DROP.",
            )
        return cast(exp.Expression, statements[0])

    @staticmethod
    def _assert_select(tree: exp.Expression) -> None:
        readonly = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)
        if not isinstance(tree, readonly):
            kind = type(tree).__name__.upper()
            raise GuardrailViolation(
                ViolationCode.NOT_A_SELECT,
                f"Only read-only SELECT statements are permitted; got {kind}.",
            )
        for node in tree.find_all(exp.Expression):
            if isinstance(
                node,
                (
                    exp.Insert,
                    exp.Update,
                    exp.Delete,
                    exp.Drop,
                    exp.Create,
                    exp.Alter,
                    exp.Merge,
                    exp.Command,
                    exp.Copy,
                    exp.Set,
                    exp.Use,
                ),
            ):
                raise GuardrailViolation(
                    ViolationCode.NOT_A_SELECT,
                    f"Statement contains a {type(node).__name__.upper()} node.",
                )

    def _check_tables(self, tree: exp.Expression) -> None:
        cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.name in cte_names and not table.db:
                continue
            qualified = f"{table.db}.{table.name}" if table.db else table.name
            allowed = (
                qualified in self.config.allowed_tables
                if table.db
                else table.name in self.config.allowed_bare_tables
            )
            if not allowed:
                raise GuardrailViolation(
                    ViolationCode.TABLE_NOT_ALLOWED,
                    f"Table {qualified!r} is not on the allowlist. Permitted: "
                    f"{sorted(self.config.allowed_tables)}",
                )

    @staticmethod
    def _check_table_source(tree: exp.Expression) -> None:
        """Require a real table somewhere in the query.

        Grounding trusts values returned by this guarded path. A tableless query is not a
        warehouse observation, so permitting ``SELECT <model supplied number>`` would let the
        model manufacture the evidence used to approve its own answer.
        """
        cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        base_tables = [
            table
            for table in tree.find_all(exp.Table)
            if table.db or table.name not in cte_names
        ]
        if not base_tables:
            raise GuardrailViolation(
                ViolationCode.TABLE_REQUIRED,
                "A grounded query must read an allowlisted warehouse table. Constant-only "
                "SELECT statements cannot establish facts.",
            )

    @staticmethod
    def _check_projected_literals(tree: exp.Expression) -> None:
        """Reject model supplied numbers projected as if the warehouse produced them.

        Numeric literals remain valid in filters, limits, grouping ordinals, and the zero
        denominator of ``NULLIF``. They are forbidden in result expressions because every
        numeric result becomes grounding evidence. The exception preserves governed ratio
        expressions such as ``sum(revenue) / nullif(sum(spend), 0)``.
        """
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                for literal in projection.find_all(exp.Literal):
                    if not literal.is_number:
                        continue
                    parent = literal.parent
                    safe_nullif_zero = (
                        literal.this == "0"
                        and isinstance(parent, exp.Nullif)
                        and parent.args.get("expression") is literal
                    )
                    if safe_nullif_zero:
                        continue
                    raise GuardrailViolation(
                        ViolationCode.LITERAL_PROJECTION,
                        f"Numeric literal {literal.sql()!r} is projected into the result. "
                        "Only values derived from warehouse columns may become grounding "
                        "facts.",
                    )

    @staticmethod
    def _check_functions(tree: exp.Expression) -> None:
        for node in tree.find_all(exp.Anonymous):
            name = str(node.this).lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise GuardrailViolation(
                    ViolationCode.FUNCTION_NOT_ALLOWED,
                    f"Function {name!r} can reach the filesystem or the network.",
                )
        for func in tree.find_all(exp.Func):
            func_name = getattr(func, "sql_name", lambda: "")().lower()
            if func_name in FORBIDDEN_FUNCTIONS:
                raise GuardrailViolation(
                    ViolationCode.FUNCTION_NOT_ALLOWED, f"Function {func_name!r} is forbidden."
                )

    @staticmethod
    def _check_star(tree: exp.Expression) -> None:
        """Reject projected stars (``select *``, ``select t.*``) but not ``count(*)``.

        The distinction is the parent node: a star inside a function is an arity marker,
        a star in a projection is an unbounded column read.
        """
        for star in tree.find_all(exp.Star):
            parent = star.parent
            if isinstance(parent, exp.Func):
                continue
            raise GuardrailViolation(
                ViolationCode.STAR_NOT_ALLOWED,
                "SELECT * is not permitted: name the columns you intend to read.",
            )

    def _check_aggregates(self, tree: exp.Expression) -> None:
        allowed = self.config.allowed_aggregates
        for node in tree.find_all(exp.AggFunc):
            rendered = node.sql(dialect=DIALECT).lower()
            if rendered in allowed:
                continue
            if isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
                continue  # count(*) is a row count, not a metric
            raise GuardrailViolation(
                ViolationCode.UNREGISTERED_AGGREGATE,
                f"Aggregate {rendered!r} is not an atom of any registered metric. "
                "Metrics are defined once, in semantic/metrics.yml. In particular, an "
                "average of a per-row ratio is not the ratio of the sums, and is wrong.",
            )

    @staticmethod
    def _apply_limit(tree: exp.Expression, n: int) -> exp.Expression:
        """Attach a LIMIT. Set operations must be wrapped in a subquery first."""
        if isinstance(tree, exp.Select):
            return cast(exp.Expression, tree.limit(n))
        wrapped = cast(exp.Query, tree).subquery("_limited")
        limited: exp.Expression = exp.select("*").from_(wrapped).limit(n)
        return limited

    def _enforce_limit(self, tree: exp.Expression, warnings: list[str]) -> exp.Expression:
        limit = tree.args.get("limit")
        if limit is None:
            return self._apply_limit(tree, self.config.default_limit)
        try:
            value = int(limit.expression.name)
        except (AttributeError, ValueError):
            warnings.append("Non-literal LIMIT; clamping to max_limit.")
            return self._apply_limit(tree, self.config.max_limit)
        if value > self.config.max_limit:
            warnings.append(
                f"LIMIT {value} exceeds max_limit {self.config.max_limit}; clamped."
            )
            return self._apply_limit(tree, self.config.max_limit)
        return tree


def guard_from_semantic_layer(layer: SemanticLayer, **overrides: object) -> SqlGuard:
    """Build a guard whose aggregate allowlist is derived from the semantic layer."""
    return SqlGuard(
        SqlGuardConfig(allowed_aggregates=layer.aggregate_atoms(), **overrides)  # type: ignore[arg-type]
    )
