"""Verify the public Agent Profile breaking migration and documentation surface."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_NOTE = ROOT / "docs" / "migration-agent-profile.md"
FORBIDDEN = re.compile(
    r"requirement_path|RequirementSpec|parse_requirement|"
    r"requirement_sections|requirement_required|requirement_invalid|"
    r"kuma\.repository\.requirements|KUMA_REQUIREMENT_PATH|"
    r"(?<![A-Za-z-])requirement(?!s?[A-Za-z.-])",
    re.IGNORECASE,
)


def _public_text_files() -> tuple[Path, ...]:
    """Return tracked-style source and documentation files covered by this gate."""
    files = [ROOT / "README.md", ROOT / "README.zh-CN.md"]
    for directory in (ROOT / "src" / "kuma", ROOT / "docs", ROOT / "examples"):
        files.extend(
            path
            for path in directory.rglob("*")
            if path.suffix in {".py", ".md", ".ipynb"} and path != MIGRATION_NOTE
        )
    return tuple(sorted(files))


def _function_parameters(path: Path, name: str) -> tuple[str, ...]:
    """Read one top-level function signature without importing the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        child
        for child in tree.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name == name
    )
    return tuple(
        argument.arg
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    )


def _class_fields(path: Path, name: str) -> tuple[str, ...]:
    """Read annotated class fields from source without executing decorators."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        child
        for child in tree.body
        if isinstance(child, ast.ClassDef) and child.name == name
    )
    return tuple(
        child.target.id
        for child in node.body
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
    )


def _verify_signatures() -> None:
    """Require only the current Agent Profile names at public source boundaries."""
    create_run = _function_parameters(ROOT / "src" / "kuma" / "api.py", "create_run")
    if "agent_profile_path" not in create_run or "requirement_path" in create_run:
        raise ValueError("create_run Agent Profile signature is stale")
    context = _class_fields(
        ROOT / "src" / "kuma" / "providers" / "base.py",
        "CaseGenerationContext",
    )
    required = {"agent_profile", "agent_profile_sections"}
    if not required.issubset(context) or {"requirement", "requirement_sections"} & set(
        context
    ):
        raise ValueError("CaseGenerationContext Agent Profile fields are stale")
    repository = ROOT / "src" / "kuma" / "repository"
    if not (repository / "agent_profiles.py").is_file():
        raise ValueError("agent_profiles.py is missing")
    if (repository / "requirements.py").exists():
        raise ValueError("retired requirements.py still exists")


def _verify_retired_terms() -> None:
    """Reject retired public API terms outside the explicit migration note."""
    matches = [
        path.relative_to(ROOT).as_posix()
        for path in _public_text_files()
        if FORBIDDEN.search(path.read_text(encoding="utf-8"))
    ]
    if matches:
        raise ValueError("retired Agent Profile terms remain: " + ", ".join(matches))


def _markdown_files() -> tuple[Path, ...]:
    """Return public Markdown entry points, guides, and example instructions."""
    return (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "examples").rglob("*.md")),
    )


def _verify_python_fences_and_links() -> None:
    """Parse Python examples and require every relative Markdown link to resolve."""
    failures: list[str] = []
    fence = re.compile(r"```python\s*\n(.*?)\n```", re.DOTALL)
    link = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for index, source in enumerate(fence.findall(text), start=1):
            try:
                ast.parse(source)
            except SyntaxError as exc:
                failures.append(f"{path.name}#{index}: {exc.msg}")
        for target in link.findall(text):
            relative = target.split("#", 1)[0]
            if not relative or "://" in relative or relative.startswith("mailto:"):
                continue
            if not (path.parent / relative).resolve().exists():
                failures.append(f"{path.relative_to(ROOT).as_posix()} -> {relative}")
    if failures:
        raise ValueError("documentation validation failed: " + "; ".join(failures))


def _verify_notebooks() -> None:
    """Parse committed notebook code while forbidding stored execution output."""
    failures: list[str] = []
    for path in sorted((ROOT / "examples").rglob("*.ipynb")):
        notebook = json.loads(path.read_text(encoding="utf-8"))
        for index, cell in enumerate(notebook.get("cells", ()), start=1):
            if cell.get("cell_type") != "code":
                continue
            if cell.get("execution_count") is not None or cell.get("outputs"):
                failures.append(f"{path.name}#{index}: committed execution state")
            try:
                ast.parse("".join(cell.get("source", ())))
            except SyntaxError as exc:
                failures.append(f"{path.name}#{index}: {exc.msg}")
    if failures:
        raise ValueError("notebook validation failed: " + "; ".join(failures))


def _verify_bilingual_structure() -> None:
    """Require paired English and Chinese guides to keep matching heading levels."""
    pairs = (
        ("api-reference.md", "api-reference.zh-CN.md"),
        ("sdk-guide.md", "sdk-guide.zh-CN.md"),
        ("strategy-groups.md", "strategy-groups.zh-CN.md"),
        ("agent-tool-capabilities.md", "agent-tool-capabilities.zh-CN.md"),
    )
    for english_name, chinese_name in pairs:
        english = (ROOT / "docs" / english_name).read_text(encoding="utf-8")
        chinese = (ROOT / "docs" / chinese_name).read_text(encoding="utf-8")
        english_levels = tuple(
            len(match.group(1))
            for match in re.finditer(r"^(#{1,6})\s+", english, re.MULTILINE)
        )
        chinese_levels = tuple(
            len(match.group(1))
            for match in re.finditer(r"^(#{1,6})\s+", chinese, re.MULTILINE)
        )
        if english_levels != chinese_levels:
            raise ValueError(f"bilingual headings differ: {english_name}")


def main() -> int:
    """Run all deterministic public Agent Profile migration checks."""
    _verify_signatures()
    _verify_retired_terms()
    _verify_python_fences_and_links()
    _verify_notebooks()
    _verify_bilingual_structure()
    print("Agent Profile migration verified: signatures, terms, docs, links, notebooks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
