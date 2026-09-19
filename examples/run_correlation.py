"""Run from this candidate source checkout; no credentials, network or model."""

import json
import tempfile

from kuma import create_run


class LocalCase:
    agent_profile_required = False

    def generate_case(self, context):
        return {"case_id": "local-demo", "inputs": ["Return the sum of 2 and 3"]}


def local_judge(context):
    return {"report_id": "local-check", "status": "pass"}


with tempfile.TemporaryDirectory() as repository:
    run = create_run(
        repo_path=repository,
        case_provider=LocalCase(),
        judge_provider=local_judge,
        max_steps=1,
        track_files=False,
        allow_local=True,
        external_run_id="demo-job",
    )
    run.get_input()
    run.submit(5, external_invocation_id="demo-call-1")
    print(json.dumps(run.timeline, indent=2))
