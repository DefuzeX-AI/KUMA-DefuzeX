"""Real library instrumentation with deterministic offline model boundaries.

From this source checkout install examples/requirements-instrumentation.txt and
KUMA editable, then select --framework openai|langchain|langgraph. No real key,
socket, cloud upload, Case or Judge is used. Optional packages are imported only
when running this example, not by KUMA itself.
"""

from __future__ import annotations

import argparse
import json
from typing import TypedDict
from unittest.mock import patch

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from kuma import observe


def openai_call() -> str:
    """Exercise real OpenAI request/response parsing, replacing only HTTP I/O."""
    import httpx2
    from openai import OpenAI

    def respond(request: httpx2.Request) -> httpx2.Response:
        """Answer the exact offline endpoint; unexpected requests fail locally."""
        assert str(request.url) == "https://offline.invalid/v1/chat/completions"
        assert json.loads(request.content)["messages"][0]["content"] == "Add 2 and 3."
        return httpx2.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "offline-example",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "5"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 6,
                    "completion_tokens": 1,
                    "total_tokens": 7,
                },
            },
        )

    with OpenAI(
        api_key="offline-placeholder",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as client:
        return (
            client.chat.completions.create(
                model="offline-example",
                messages=[{"role": "user", "content": "Add 2 and 3."}],
            )
            .choices[0]
            .message.content
        )


def langchain_call() -> str:
    """Invoke a real Runnable/tool/chat-model stack with a deterministic fake model."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers locally without files or external services."""
        return a + b

    model = FakeListChatModel(responses=["5"])

    def calculate(value: dict) -> str:
        """Execute a real local tool and pass its result through the model callback."""
        total = add.invoke(value)
        return model.invoke(f"Report {total}.").content

    return RunnableLambda(calculate).invoke({"a": 2, "b": 3})


class CalculationState(TypedDict):
    """Public in-memory graph state, independent of KUMA evaluation contracts."""

    a: int
    b: int
    total: int
    answer: str


def langgraph_call() -> str:
    """Run two actual graph nodes with a real tool and deterministic model boundary."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers without network or persistent state."""
        return a + b

    model = FakeListChatModel(responses=["5"])

    def calculate(state: CalculationState) -> dict:
        """Update only the total using an observable framework tool call."""
        return {"total": add.invoke({"a": state["a"], "b": state["b"]})}

    def answer(state: CalculationState) -> dict:
        """Produce the final graph answer through the instrumented model callback."""
        return {"answer": model.invoke(f"Report {state['total']}.").content}

    graph = StateGraph(CalculationState)
    graph.add_node("calculate", calculate)
    graph.add_node("answer", answer)
    graph.add_edge(START, "calculate")
    graph.add_edge("calculate", "answer")
    graph.add_edge("answer", END)
    return graph.compile().invoke({"a": 2, "b": 3})["answer"]


def run_sample(framework: str) -> dict:
    """Observe real library spans while blocking all outbound sockets.

    The example explicitly owns its instrumentor/provider and removes its patches
    afterward. KUMA does not install instrumentation, exporters or global state.
    The returned export is local and may correctly report partial content when
    instrumentation emits fields excluded by the existing privacy allowlist.
    """
    if framework == "openai":
        from openinference.instrumentation.openai import OpenAIInstrumentor

        instrumentor = OpenAIInstrumentor()
        call = openai_call
    elif framework in {"langchain", "langgraph"}:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        instrumentor = LangChainInstrumentor()
        call = langchain_call if framework == "langchain" else langgraph_call
    else:
        raise ValueError("Choose openai, langchain or langgraph")
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    instrumentor.instrument(tracer_provider=provider)
    try:
        with (
            patch(
                "socket.socket.connect", side_effect=AssertionError("Network forbidden")
            ),
            observe(
                tracer_provider=provider, external_run_id=f"offline-{framework}"
            ) as captured,
        ):
            result = call()
        return {"result": result, "observation": captured.export()}
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def main() -> None:
    """Print a local normalized capture for exactly one chosen offline framework."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--framework", required=True, choices=("openai", "langchain", "langgraph")
    )
    print(json.dumps(run_sample(parser.parse_args().framework), indent=2))


if __name__ == "__main__":
    main()
