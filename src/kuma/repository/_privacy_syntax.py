"""Bounded, non-executing source recognition shared by privacy scan/redaction."""

from __future__ import annotations

import ast
import re

_REFERENCE_NAME = re.compile(
    r"(?i)(?:.*_)?(?:password|passwd|token|key|auth|credentials?)$"
)
_GETTER = re.compile(
    r"(?i)get_(?:[a-z0-9]+_)*(?:password|passwd|token|key|auth|credentials?)$"
)


def _name(node: ast.AST) -> str:
    """Return a dotted source name without resolving attributes or importing it."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return parent + "." + node.attr if parent else ""
    return ""


def _reference(node: ast.AST) -> bool:
    """Accept credential references, not arbitrary calls or embedded literals.

    This is a syntax-only allowlist for source RHS spans. Environment accessors
    may contain a variable name and an empty default; credential getter calls
    may contain only name/attribute arguments. Literal defaults, concatenated
    secrets and arbitrary function calls remain subject to normal scanning.
    """
    if isinstance(node, ast.Name):
        return _REFERENCE_NAME.fullmatch(node.id) is not None
    if isinstance(node, ast.Attribute):
        return bool(_name(node))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return all(_reference(value) for value in node.values)
    if not isinstance(node, ast.Call) or node.keywords:
        return False
    name = _name(node.func)
    if name in {"os.getenv", "os.environ.get"}:
        return (
            1 <= len(node.args) <= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and re.fullmatch(r"[A-Z_][A-Z0-9_]*", node.args[0].value) is not None
            and (
                len(node.args) == 1
                or (isinstance(node.args[1], ast.Constant) and node.args[1].value == "")
            )
        )
    return bool(_GETTER.fullmatch(name)) and all(
        isinstance(arg, (ast.Name, ast.Attribute)) and bool(_name(arg))
        for arg in node.args
    )


def source_value_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Locate safe source RHS values for assignment and header classifiers.

    Args:
        text: Bounded upload text, possibly containing source lines. Each line
            is parsed independently only at <=4096 characters and <=256 AST
            nodes; malformed/large fragments receive no exemption.

    Returns:
        Character-offset spans for credential references, boolean use_* options,
        or header concatenation with a credential getter. AST offsets are UTF-8
        bytes and are converted before matching the scanner's character spans.

    Security/Privacy:
        Never execute code or resolve environment/filesystem data. No whole
        statement is exempted: adjacent literal credentials and independent
        token signatures remain visible to the canonical scanner/redactor.
    """
    spans = []
    offset = 0
    previous = ""
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        leading = len(raw) - len(raw.lstrip())
        if len(line) <= 4096 and any(mark in line for mark in ("=", ":")):
            try:
                tree = ast.parse(line)
            except (SyntaxError, ValueError, RecursionError, UnicodeError):
                tree = None
            if tree is not None:
                nodes = list(ast.walk(tree))
                if len(nodes) <= 256:
                    for node in nodes:
                        spans.extend(
                            _span(line, value, offset + leading)
                            for value in _reference_values(node)
                        )
                    spans.extend(
                        _span(line, target, offset + leading)
                        for target in _conditional_reference_targets(previous, tree)
                    )
        previous = line
        offset += len(raw)
    return tuple(spans)


def _conditional_reference_targets(
    previous: str, tree: ast.Module
) -> tuple[ast.AST, ...]:
    """Recognize a reference assignment immediately after a bounded if header.

    The text credential matcher may consume a condition's trailing colon and
    the following assignment target as its value. Exempt only that target when
    the previous line is a name/attribute boolean condition and the sole current
    assignment already has a safe reference RHS. Literal RHS, calls in the
    condition, malformed or oversized source receive no exemption. This helper
    never executes source; independent signatures still scan the full text.
    """
    if (
        len(previous) > 4096
        or not previous.startswith("if ")
        or not previous.endswith(":")
    ):
        return ()
    try:
        header = ast.parse(previous + "\n pass")
    except (SyntaxError, ValueError, RecursionError, UnicodeError):
        return ()
    # The caller supplies one stripped physical line. Successful parsing of its
    # "if ...:" prefix plus the appended suite guarantees one ast.If statement.
    condition = header.body[0]
    nodes = list(ast.walk(condition.test))
    if len(nodes) > 256 or any(
        not isinstance(
            node,
            (
                ast.Name,
                ast.Attribute,
                ast.BoolOp,
                ast.UnaryOp,
                ast.And,
                ast.Or,
                ast.Not,
                ast.Load,
            ),
        )
        for node in nodes
    ):
        return ()
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return ()
    assignment = tree.body[0]
    if not _reference(assignment.value):
        return ()
    return tuple(
        target for target in assignment.targets if isinstance(target, ast.Name)
    )


def _reference_values(node: ast.AST) -> tuple[ast.AST, ...]:
    """Select individual reference RHS nodes, not containing statements/literals."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and _reference(node.value):
        if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
            return (node.value, node.annotation)
        return (node.value,)
    if (
        isinstance(node, ast.keyword)
        and node.arg
        and node.arg.startswith("use_")
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is bool
    ):
        return (node.value,)
    if isinstance(node, ast.Dict):
        return tuple(
            value
            for key, value in zip(node.keys, node.values, strict=True)
            if _header_reference(key, value)
        )
    return ()


def _header_reference(key: ast.AST | None, value: ast.AST) -> bool:
    """Recognize a header scheme concatenated with a reference, not a secret literal."""
    return (
        isinstance(key, ast.Constant)
        and key.value == "Authorization"
        and isinstance(value, ast.BinOp)
        and isinstance(value.op, ast.Add)
        and isinstance(value.left, ast.Constant)
        and value.left.value in ("Bearer ", "Basic ")
        and _reference(value.right)
    )


def _span(line: str, node: ast.AST, offset: int) -> tuple[int, int]:
    """Translate one-line AST byte positions to absolute text character offsets."""
    encoded = line.encode("utf-8")
    end = len(encoded[: node.end_col_offset].decode("utf-8"))
    while end < len(line) and line[end] == ")":
        end += 1
    return (
        offset + len(encoded[: node.col_offset].decode("utf-8")),
        offset + end,
    )
