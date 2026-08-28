"""Run the real SDK + mini-swe-agent flow inside one Docker container."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from pprint import pprint

import yaml
from minisweagent import __version__ as mini_swe_version
from minisweagent import package_dir as mini_swe_package_dir
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from kuma import create_run
from kuma.otel import configure_trace_evidence

REPO = Path("/workspace").resolve()
REQUIREMENT = REPO / "requirement.md"
ARTIFACTS = REPO / ".kuma" / "mini-swe-agent"

SEQUENTIAL_STEP_TEMPLATE = """Complete exactly this one KUMA Case step:

<case_step>{{task}}</case_step>
<execution_mode>{{execution_mode}}</execution_mode>

The Case step is authoritative. Do not continue into later diagnosis, repair,
verification, or reporting work that it does not request. Do not inspect `.kuma`,
invent hidden requirements, modify tests, add dependencies, or access paths outside
the repository.

If execution_mode is `read_only`, you may inspect files and run existing commands or
tests, but you must not write, create, delete, or modify files. If execution_mode is
`edit_calculator`, you may modify only `calculator.py`; inspect first and make only
the smallest evidence-backed change requested by the current step.

Keep reasoning to at most two short sentences. Every response must contain exactly
one Bash action in the format required by the system message. As soon as this one
step is complete, submit with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` as its own
action. Submit even when the step is observational or no change is required.

<system_information>
{{system}} {{release}} {{version}} {{machine}}
</system_information>
"""

EDIT_STEP = re.compile(
    r"^\s*(?:\d+[.)]\s*)?"
    r"(?:apply|correct|create|delete|edit|fix|implement|make|modify|patch|remove|"
    r"replace|update|write)\b",
    re.IGNORECASE,
)


class TracedLocalEnvironment(LocalEnvironment):
    """Add bounded OTel metadata around mini-swe-agent's native Bash tool."""

    def execute(
        self, action: dict, cwd: str = "", *, timeout: int | None = None
    ) -> dict:
        started = time.perf_counter()
        with tracer.start_as_current_span("agent.mini_swe.tool") as span:
            span.set_attribute("agent.framework", "mini-swe-agent")
            span.set_attribute("agent.tool.name", "bash")
            try:
                output = super().execute(action, cwd=cwd, timeout=timeout)
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("agent.tool.exit_code", output["returncode"])
            span.set_attribute(
                "agent.tool.duration_ms",
                int((time.perf_counter() - started) * 1000),
            )
            span.set_status(
                Status(StatusCode.OK if output["returncode"] == 0 else StatusCode.ERROR)
            )
            return output


def python_snapshot(repo: Path) -> dict[str, str]:
    return {
        path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(repo.glob("*.py"))
    }


def execution_mode(task: str) -> str:
    """Grant write access only when the Case step begins with an edit action."""

    return "edit_calculator" if EDIT_STEP.match(task) else "read_only"


def write_evidence_log(
    evidence_log: Path,
    trajectory_path: Path,
    test_run: subprocess.CompletedProcess[str],
) -> None:
    """Combine one Agent trajectory and its verification into one upload file."""

    raw_trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    info = raw_trajectory["info"]
    compact_trajectory = {
        "mini_version": info["mini_version"],
        "exit_status": info["exit_status"],
        "model_stats": info["model_stats"],
        "messages": [
            {"role": message["role"], "content": message.get("content", "")}
            for message in raw_trajectory["messages"]
        ],
        "trajectory_format": raw_trajectory["trajectory_format"],
    }
    evidence_log.write_text(
        json.dumps(
            {
                "trajectory": compact_trajectory,
                "verification": {
                    "command": [sys.executable, "-m", "unittest", "discover", "-v"],
                    "exit_code": test_run.returncode,
                    "output": test_run.stdout + test_run.stderr,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def run_verification(
    test_log: Path,
    evidence_log: Path,
    trajectory_path: Path,
) -> bool:
    """Run the repository tests and persist their OTel-backed evidence."""

    with tracer.start_as_current_span("agent.tests") as test_span:
        test_run = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-v"],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        test_span.set_attribute("agent.tool.name", "python.unittest")
        test_span.set_attribute("agent.tool.exit_code", test_run.returncode)
        test_span.set_status(
            Status(StatusCode.OK if test_run.returncode == 0 else StatusCode.ERROR)
        )
    test_log.write_text(test_run.stdout + test_run.stderr, encoding="utf-8")
    write_evidence_log(evidence_log, trajectory_path, test_run)
    return test_run.returncode == 0


def run_mini_swe_agent(task: str, step_index: int) -> dict:
    """Execute unmodified mini-swe-agent planning inside this same container."""

    before = python_snapshot(REPO)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    trajectory_path = ARTIFACTS / f"trajectory-{step_index}.json"
    test_log = ARTIFACTS / f"unittest-{step_index}.log"
    evidence_log = ARTIFACTS / f"evidence-{step_index}.json"

    config = yaml.safe_load(
        (mini_swe_package_dir / "config" / "default.yaml").read_text(encoding="utf-8")
    )
    agent_config = dict(config["agent"])
    agent_config["instance_template"] = SEQUENTIAL_STEP_TEMPLATE
    agent_config.update(
        step_limit=10,
        cost_limit=1.0,
        wall_time_limit_seconds=180,
        max_consecutive_format_errors=6,
        output_path=trajectory_path,
    )
    configured_model = os.environ.get("KUMA_DEEPSEEK_MODEL", "deepseek-chat")
    model_name = (
        configured_model if "/" in configured_model else f"deepseek/{configured_model}"
    )
    model_config = dict(config["model"])
    model_config.update(model_name=model_name, cost_tracking="ignore_errors")
    model_config["model_kwargs"] = {
        **model_config.get("model_kwargs", {}),
        "max_tokens": 1024,
        "temperature": 0,
    }
    agent = DefaultAgent(
        LitellmTextbasedModel(**model_config),
        TracedLocalEnvironment(cwd=str(REPO), timeout=30, **config["environment"]),
        **agent_config,
    )

    started = time.perf_counter()
    with tracer.start_as_current_span("agent.mini_swe.run") as span:
        span.set_attribute("agent.framework", "mini-swe-agent")
        span.set_attribute("agent.framework.version", mini_swe_version)
        span.set_attribute("gen_ai.request.model", model_name)
        try:
            mode = execution_mode(task)
            span.set_attribute("agent.execution_mode", mode)
            result = agent.run(task, execution_mode=mode)
            span.set_attribute("agent.model_calls", agent.n_calls)
            span.set_attribute("agent.exit_status", result.get("exit_status", ""))
            span.set_status(
                Status(
                    StatusCode.OK
                    if result.get("exit_status") == "Submitted"
                    else StatusCode.ERROR
                )
            )
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            span.set_attribute(
                "agent.duration_ms", int((time.perf_counter() - started) * 1000)
            )

    if result.get("exit_status") != "Submitted":
        raise RuntimeError(
            f"mini-swe-agent did not submit successfully: {result.get('exit_status')}"
        )
    after = python_snapshot(REPO)
    changed = sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )
    unexpected = [path for path in changed if path != "calculator.py"]
    if unexpected:
        raise RuntimeError(f"mini-swe-agent changed out-of-scope files: {unexpected}")
    if execution_mode(task) == "read_only" and changed:
        raise RuntimeError(
            f"mini-swe-agent changed files during a read-only Case step: {changed}"
        )

    tests_passed = run_verification(test_log, evidence_log, trajectory_path)
    return {
        "agent": "mini-swe-agent",
        "agent_version": mini_swe_version,
        "model": model_name,
        "exit_status": result["exit_status"],
        "summary": result.get("submission", ""),
        "changed_files": changed,
        "tests_passed": tests_passed,
        "model_calls": agent.n_calls,
        "trajectory_log": trajectory_path,
        "test_log": test_log,
        "evidence_log": evidence_log,
    }


if not Path("/.dockerenv").exists():
    raise RuntimeError("This user-flow entry point must run inside Docker.")
for required_name in ("KUMA_BASE_URL", "KUMA_API_KEY", "DEEPSEEK_API_KEY"):
    if not os.environ.get(required_name):
        raise RuntimeError(f"Missing required environment variable: {required_name}")
if not REQUIREMENT.is_file():
    raise RuntimeError("Mount the prepared Agent repository at /workspace.")

provider = TracerProvider()
trace_evidence = configure_trace_evidence(provider)
tracer = provider.get_tracer("official-mini-swe-agent")

# No allow_local escape hatch: create_run must detect Docker and acquire the
# container-wide active-Run lock before the official Case can be delivered.
run = create_run(
    repo_path=REPO,
    requirement_path=REQUIREMENT,
    track_files=True,
    upload_diff=False,
    save_local=True,
    trace_evidence=trace_evidence,
)

report = None
step_index = 0
while (test_input := run.get_input(full=True)) is not None:
    print("Official Case input:")
    pprint(test_input.payload)
    result = run_mini_swe_agent(str(test_input.payload), step_index)
    log_keys = {"evidence_log", "test_log", "trajectory_log"}
    output = {key: value for key, value in result.items() if key not in log_keys}
    # mini-swe-agent does not emit the standard OTel Agent/Workflow final-output
    # attribute, so this example uses the SDK's explicit-output fallback.
    report = run.submit(
        output,
        logs=[result["evidence_log"]],
    )
    step_index += 1

print("Final Judge report:")
pprint(report)
if report is None:
    raise RuntimeError("The official Judge did not return a report.")
if not all(item.submission.status == "completed" for item in run.history):
    raise RuntimeError("At least one Submission did not complete.")

# Persist only the public Judgment contract for the host-side Markdown report.
# Private Rubric content and provider extensions deliberately stay in Core MCP.
judge_report_path = ARTIFACTS / "judge-report.json"
judge_report_path.write_text(
    json.dumps(
        {
            "report_id": report.report_id,
            "run_id": report.run_id,
            "status": report.status,
            "confidence": report.confidence,
            "stop_reason": report.stop_reason,
            "issues": [dict(item) for item in report.issues],
            "evidence_gaps": [dict(item) for item in report.evidence_gaps],
            "schema_version": report.schema_version,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

print("Captured KUMA Trace Evidence:")
for item in run.history:
    evidence = item.submission.extensions.get("trace_evidence")
    if not evidence or evidence["schema_version"] != "defuzex.trace_evidence.v1":
        raise RuntimeError("Trace Evidence was not captured.")
    print(item.test_input.input_id, "spans=", len(evidence["spans"]))
    print([span["name"] for span in evidence["spans"]])

print("Final calculator.py:")
print((REPO / "calculator.py").read_text(encoding="utf-8"))
print("Public Judge artifact:", judge_report_path)
print("Runtime mode: docker (SDK and Agent share this container)")
