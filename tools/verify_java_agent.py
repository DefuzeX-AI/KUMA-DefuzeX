"""Exercise the compiled Java example over real OTLP, including failure boundaries."""

from __future__ import annotations

import os
import runpy
import subprocess
from pathlib import Path

from kuma.otlp import OtlpTraceReceiver

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/java_agent"


def verify_failure(input_value: str, *, rejected_export: bool) -> None:
    """Require no result on export failure and truthful spans on an executed tool error."""
    classpath = (
        str(EXAMPLE / "target/classes")
        + os.pathsep
        + (EXAMPLE / "target/classpath.txt").read_text().strip()
    )
    with OtlpTraceReceiver() as receiver:
        receiver.capture.begin_step("java-check", "local-case", "step-1")
        with receiver.step() as settings:
            endpoint = settings["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
            header = settings["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split("=", 1)[1]
            if rejected_export:
                header = "Bearer deliberately-invalid-local-test-token"
            result = subprocess.run(
                ["java", "-cp", classpath, "ExampleAgent"],
                input=f"{endpoint}\n{header}\n{input_value}\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=20,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(("OTEL_", "KUMA_", "DEFUZEX_"))
                },
            )
        if result.returncode == 0 or result.stdout.strip():
            raise RuntimeError("Java failure incorrectly produced a successful result")
        spans = receiver.capture.prepare_step(
            "java-check", "local-case", "step-1"
        ).evidence["spans"]
        if rejected_export:
            if spans or "Trace export failed" not in result.stderr:
                raise RuntimeError("Java failed to surface a rejected Trace export")
        else:
            statuses = {
                span["attributes"].get("gen_ai.operation.name"): span["status"]
                for span in spans
            }
            if (
                statuses
                != {
                    "invoke_agent": "error",
                    "execute_tool": "error",
                    "chat": "ok",
                }
                or "ArithmeticException" not in result.stderr
            ):
                raise RuntimeError("Java tool failure lost its observed error status")
            tool = next(
                span
                for span in spans
                if span["attributes"].get("gen_ai.operation.name") == "execute_tool"
            )
            if "gen_ai.tool.call.result" in tool["attributes"]:
                raise RuntimeError("Java failed tool incorrectly recorded a result")


def main() -> None:
    """Run the free two-input example and real export/tool-error checks after Maven build."""
    runpy.run_path(str(EXAMPLE / "run.py"))["main"]()
    verify_failure("2", rejected_export=True)
    verify_failure("2147483647", rejected_export=False)
    print(
        "Java OTLP checks passed: session, model/tool bodies, export rejection, tool error"
    )


if __name__ == "__main__":
    main()
