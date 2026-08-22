# Contributing to KUMA Python SDK

Thank you for helping improve the public SDK. Keep changes within the client's
responsibilities: public protocol models, transport behavior, Run lifecycle,
Evidence construction, and Provider interfaces. Backend, private Core MCP,
model execution, credentials, prompts, and databases do not belong here.

## Development setup

Use Python 3.10 or newer in an isolated environment:

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Activate the environment using the command appropriate for your shell, then
run the canonical checks:

```bash
python -m ruff format --check --exclude "*.ipynb" .
python -m ruff check --exclude "*.ipynb" .
python -m compileall -q src examples
defuzex quickstart
python examples/minimal_local.py
python -m build
python -m twine check dist/*
```

Public CI checks lint, supported-Python installation and imports, the CLI,
offline examples, and package construction. This public repository does not
ship the maintainers' complete security and contract regression suite.
Maintainers run those private checks before accepting a release.

## Pull requests

- Use a fork or a focused branch; do not commit directly to `main`.
- Keep each pull request limited to one confirmed behavior, defect, or document.
- Preserve the public contract and describe reproducible validation for behavior
  changes.
- Do not weaken validation, hide failures, or include generated build artifacts.
- Never commit API keys, `.env` files, local paths, private Rubrics, model prompts,
  service addresses, databases, or user data.
- Explain compatibility, security, and privacy effects in the pull request.

By submitting a contribution, you agree that it is licensed under the Apache
License 2.0. Report security issues privately as described in
[SECURITY.md](SECURITY.md), not through a public issue.
