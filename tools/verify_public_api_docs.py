"""Fail when documented public API parameters drift from source signatures."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs/api-reference.md", ROOT / "docs/api-reference.zh-CN.md")
MARKER = re.compile(
    r"<!-- api-parameters:(?P<name>[A-Za-z_][A-Za-z0-9_]*):start -->"
    r"(?P<body>.*?)"
    r"<!-- api-parameters:(?P=name):end -->",
    re.DOTALL,
)


@dataclass(frozen=True)
class PublicApi:
    """Describe one source callable or dataclass documented in both references."""

    name: str
    source: str
    qualified_name: str
    dataclass_fields: bool = False
    required_docstring_sections: tuple[str, ...] = (
        "Args",
        "Returns",
        "Raises",
        "Preconditions",
        "Postconditions",
        "Side Effects",
        "Security/Privacy",
    )


PUBLIC_APIS = (
    PublicApi("configure", "src/kuma/api.py", "configure"),
    PublicApi("create_run", "src/kuma/api.py", "create_run"),
    PublicApi(
        "parse_agent_profile",
        "src/kuma/repository/agent_profiles.py",
        "parse_agent_profile",
    ),
    PublicApi("get_input", "src/kuma/run.py", "Run.get_input"),
    PublicApi("submit", "src/kuma/run.py", "Run.submit"),
    PublicApi("judge", "src/kuma/run.py", "Run.judge"),
    PublicApi(
        "KumaClient",
        "src/kuma/client.py",
        "KumaClient.__init__",
        required_docstring_sections=(
            "Args",
            "Raises",
            "Preconditions",
            "Postconditions",
            "Side Effects",
            "Security/Privacy",
        ),
    ),
    PublicApi(
        "configure_trace_evidence",
        "src/kuma/otel.py",
        "configure_trace_evidence",
    ),
    PublicApi(
        "TraceEvidenceLimits",
        "src/kuma/evidence/trace.py",
        "TraceEvidenceLimits",
        dataclass_fields=True,
        required_docstring_sections=(
            "Args",
            "Raises",
            "Preconditions",
            "Postconditions",
            "Security/Privacy",
        ),
    ),
)


def _find_node(tree: ast.AST, qualified_name: str) -> ast.AST:
    """Resolve a top-level function, class method, or class by dotted name."""
    current: ast.AST = tree
    for part in qualified_name.split("."):
        body = getattr(current, "body", ())
        current = next(
            (
                node
                for node in body
                if isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and node.name == part
            ),
            None,
        )
        if current is None:
            raise ValueError(f"cannot find {qualified_name}")
    return current


def _callable_parameters(node: ast.AST) -> tuple[str, ...]:
    """Return source-order user parameters while excluding method receivers."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise TypeError("expected a function node")
    args = node.args
    names = [item.arg for item in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if names and names[0] in {"self", "cls"}:
        names.pop(0)
    if args.vararg is not None:
        names.append(args.vararg.arg)
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return tuple(names)


def _dataclass_parameters(node: ast.AST) -> tuple[str, ...]:
    """Return annotated public dataclass fields in declaration order."""
    if not isinstance(node, ast.ClassDef):
        raise TypeError("expected a class node")
    return tuple(
        child.target.id
        for child in node.body
        if isinstance(child, ast.AnnAssign)
        and isinstance(child.target, ast.Name)
        and not child.target.id.startswith("_")
    )


def _source_contract(api: PublicApi) -> tuple[tuple[str, ...], str]:
    """Read one public signature and its owning callable or class docstring."""
    tree = ast.parse((ROOT / api.source).read_text(encoding="utf-8"))
    node = _find_node(tree, api.qualified_name)
    parameters = (
        _dataclass_parameters(node)
        if api.dataclass_fields
        else _callable_parameters(node)
    )
    doc_owner = (
        _find_node(tree, api.qualified_name.removesuffix(".__init__"))
        if api.qualified_name.endswith(".__init__")
        else node
    )
    docstring = ast.get_docstring(doc_owner) or ""
    return parameters, docstring


def _documented_tables(path: Path) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Parse marked four-column parameter tables from one Markdown reference."""
    text = path.read_text(encoding="utf-8")
    tables: dict[str, tuple[tuple[str, ...], ...]] = {}
    for match in MARKER.finditer(text):
        rows: list[tuple[str, ...]] = []
        for line in match.group("body").splitlines():
            if not line.lstrip().startswith("| `"):
                continue
            cells = tuple(
                cell.strip().replace(r"\|", "|")
                for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
            )
            if len(cells) != 4:
                raise ValueError(
                    f"{path}: {match.group('name')} row must have 4 columns"
                )
            rows.append(cells)
        tables[match.group("name")] = tuple(rows)
    return tables


def _parameter_name(cell: str) -> str:
    """Extract the identifier from a backtick-delimited parameter cell."""
    if not (cell.startswith("`") and cell.endswith("`")):
        raise ValueError(f"parameter name must use backticks: {cell}")
    return cell[1:-1]


def _validate_table(
    path: Path,
    name: str,
    expected_parameters: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> list[str]:
    """Validate one marked table against a source-order parameter list."""
    errors: list[str] = []
    try:
        actual_parameters = tuple(_parameter_name(row[0]) for row in rows)
    except ValueError as exc:
        return [f"{path}: {name}: {exc}"]
    if actual_parameters != expected_parameters:
        errors.append(
            f"{path}: {name} parameters {actual_parameters} "
            f"!= source {expected_parameters}"
        )
    for parameter, row in zip(actual_parameters, rows, strict=True):
        if any(not cell for cell in row[1:]):
            errors.append(f"{path}: {name}.{parameter} has an empty field")
        if len(row[3]) < 20:
            errors.append(f"{path}: {name}.{parameter} description is too vague")
    return errors


def _validate_document(
    path: Path, source_contracts: dict[str, tuple[tuple[str, ...], str]]
) -> list[str]:
    """Validate marker coverage and every parameter table in one language."""
    try:
        tables = _documented_tables(path)
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    expected_targets = set(source_contracts)
    if set(tables) != expected_targets:
        errors.append(
            f"{path}: marker set {sorted(tables)} != {sorted(expected_targets)}"
        )
    for name, (parameters, _) in source_contracts.items():
        errors.extend(_validate_table(path, name, parameters, tables.get(name, ())))
    return errors


def _validate_source_docstrings(
    source_contracts: dict[str, tuple[tuple[str, ...], str]],
) -> list[str]:
    """Require public parameters and applicable contract sections in source docs."""
    errors: list[str] = []
    api_by_name = {api.name: api for api in PUBLIC_APIS}
    for name, (parameters, docstring) in source_contracts.items():
        if not docstring:
            errors.append(f"source docstring missing for {name}")
            continue
        errors.extend(
            f"source docstring for {name} omits {parameter}"
            for parameter in parameters
            if parameter not in docstring
        )
        headings = {
            line.strip()[:-1]
            for line in docstring.splitlines()
            if line.strip().endswith(":")
        }
        errors.extend(
            f"source docstring for {name} omits {section} section"
            for section in api_by_name[name].required_docstring_sections
            if section not in headings
        )
    return errors


def main() -> int:
    """Validate bilingual table coverage, order, content, and source docstrings."""
    source_contracts = {api.name: _source_contract(api) for api in PUBLIC_APIS}
    errors = _validate_source_docstrings(source_contracts)

    for path in DOCS:
        errors.extend(_validate_document(path, source_contracts))

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Public API documentation verified: {len(PUBLIC_APIS)} APIs, "
        f"{len(DOCS)} languages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
