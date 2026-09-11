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
python tools/verify_public_api_docs.py
python tools/verify_executed_strategy_group.py
python -m compileall -q src examples tools
kuma quickstart
python examples/minimal_local.py
python -m build
python -m twine check dist/*
```

Public CI checks lint, supported-Python installation and imports, the CLI,
offline examples, and package construction. This public repository does not
ship the maintainers' complete security and contract regression suite.
Maintainers run those private checks before accepting a release.

The execution-group regression check uses synthetic responses and temporary
repositories. It needs no account or network and exercises Run metadata,
invalid-response recovery without another Case POST, and saved Case reuse.

## Pull requests

- Use a fork or a focused branch; do not commit directly to `main`.
- Keep each pull request limited to one confirmed behavior, defect, or document.
- Preserve the public contract and describe reproducible validation for behavior
  changes.
- Do not weaken validation, hide failures, or include generated build artifacts.
- Never commit API keys, `.env` files, local paths, private Rubrics, model prompts,
  service addresses, databases, or user data.
- Explain compatibility, security, and privacy effects in the pull request.
- Keep public API documentation synchronized with signatures and behavior. Each
  argument must state its type, required/default value, units or accepted range,
  sentinel meaning, and security-relevant effects. Document returns, stable
  exceptions, state/resource side effects, retries, and a minimal verified
  example where those details are not obvious. Run
  `python tools/verify_public_api_docs.py`; it detects signature/table drift but
  does not replace human semantic review.

By submitting a contribution, you agree that it is licensed under the Apache
License 2.0. Report security issues privately as described in
[SECURITY.md](SECURITY.md), not through a public issue.
