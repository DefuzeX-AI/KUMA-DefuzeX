"""Run two local KUMA Inputs against one Java process with real OTLP Trace capture."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

from kuma import create_run
from kuma.otlp import OtlpTraceReceiver


def _read_lines(stream, destination):
    """Read Java stdout into a queue so each invocation can enforce its own deadline."""
    for line in stream:
        destination.put(line)
    destination.put(None)


def _local_case(_context):
    """Supply two distinct inputs without an official generation request."""
    return {
        "case_id": "java-local-add",
        "input_type": "text",
        "inputs": [
            {"input_id": f"step-{i}", "payload_type": "text", "payload": str(i + 1)}
            for i in range(1, 3)
        ],
    }


def _verify_history(run):
    """Validate captured model/tool bodies and report the two-step local witness."""
    for item in run.history:
        spans = item.submission.extensions["trace_evidence"]["spans"]
        model = next(span for span in spans if "model_content" in span)
        tool = next(span for span in spans if "tool_content_status" in span)
        assert model["model_content"]["input"]["status"] == "present"
        assert model["model_content"]["output"]["status"] == "present"
        assert (
            tool["attributes"]["gen_ai.tool.call.result"]["sum"]
            == item.submission.output["sum"]
        )
        assert all(
            span["resource"]["telemetry.sdk.language"] == "java" for span in spans
        )
        print(
            f"{item.test_input.input_id}: spans={len(spans)} model=recorded tool=recorded"
        )
    print(
        f"run_state={run.state}; same_java_process=2_invocations; external_model_calls=0"
    )


def _start_java(classpath, run):
    """Launch the persistent process, releasing the Run if Java cannot start."""
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OTEL_", "KUMA_", "DEFUZEX_"))
    }
    try:
        return subprocess.Popen(
            ["java", "-cp", classpath, "ExampleAgent"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
    except OSError:
        run.cancel()
        raise


def main() -> int:
    """Verify Java model/tool Evidence and persistent session state without paid services."""
    root = Path(__file__).resolve().parent
    classpath_file = root / "target/classpath.txt"
    if not classpath_file.is_file():
        raise SystemExit(
            "Build first: mvn package dependency:build-classpath -Dmdep.outputFile=target/classpath.txt"
        )
    classpath = (
        str(root / "target/classes") + os.pathsep + classpath_file.read_text().strip()
    )
    with (
        tempfile.TemporaryDirectory(prefix="kuma-java-") as directory,
        OtlpTraceReceiver() as receiver,
    ):
        repo = Path(directory)
        profile = repo / "agent.md"
        profile.write_text(
            "---\nagent_description: A persistent Java addition Agent\ninput_type: text\n---\n"
            "\n## Production Use Scenario\nAdd three to each supplied integer.\n"
            "\n## Behaviors to Test\nReturn the observed sum and invocation number.\n"
            "\n## Known Limitations or Prohibited Behaviors\nUse local fixtures only.\n",
            encoding="utf-8",
        )
        run = create_run(
            repo_path=repo,
            agent_profile_path=profile,
            case_provider=_local_case,
            max_steps=2,
            judge=False,
            allow_local=True,
            track_files=False,
            trace_evidence=receiver.capture,
        )
        process = _start_java(classpath, run)
        lines = queue.Queue()
        reader = threading.Thread(
            target=_read_lines, args=(process.stdout, lines), daemon=True
        )
        reader.start()
        try:
            for invocation in range(1, 3):
                test_input = run.get_input()
                with receiver.step() as settings:
                    endpoint = settings["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
                    authorization = settings["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split(
                        "=", 1
                    )[1]
                    process.stdin.write(f"{endpoint}\n{authorization}\n{test_input}\n")
                    process.stdin.flush()
                    line = lines.get(timeout=20)
                    if line is None:
                        raise RuntimeError(
                            "Java Agent stopped before returning its result"
                        )
                    result = json.loads(line)
                    if result != {"sum": int(test_input) + 3, "invocation": invocation}:
                        raise RuntimeError("Java Agent returned an unexpected result")
                run.submit(result)
            _verify_history(run)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            process.stdout.close()
            if run.state in {"ready", "input_delivered"}:
                run.cancel()
        if process.returncode:
            raise RuntimeError("Java Agent exited unsuccessfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
