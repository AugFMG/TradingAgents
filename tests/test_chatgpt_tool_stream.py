"""Parallel tool arguments must survive ChatGPT subscription streaming."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseTextConfig,
    ResponseUsage,
)

from tradingagents.agents.schemas import PortfolioDecision
from tradingagents.agents.tools import get_macro_indicators, get_prediction_markets
from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI


class Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *args):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def __aiter__(self):
        for event in self.events:
            yield event


@pytest.fixture(scope="module")
def event_loop():
    # Windows creates a loopback socket pair for its event loop. Build it before
    # the per-test network guard; all actual HTTP clients remain mocked.
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def response(output):
    return Response.model_construct(
        id="resp_test", created_at=0, model="gpt-6-luna", object="response",
        status="completed", output=output, error=None, incomplete_details=None,
        service_tier=None, metadata={},
        text=ResponseTextConfig(format={
            "type": "json_schema", "name": "PortfolioDecision",
            "schema": PortfolioDecision.model_json_schema(), "strict": True,
        }),
        usage=ResponseUsage(
            input_tokens=11, output_tokens=4, total_tokens=15,
            input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens_details={"reasoning_tokens": 0},
        ),
    )


def client(monkeypatch, events):
    llm = ChatGPTPlanChatOpenAI(model="gpt-6-luna", api_key="test", use_responses_api=True)
    create = Mock(side_effect=lambda **kw: Stream(events))
    async_create = AsyncMock(side_effect=lambda **kw: Stream(events))
    monkeypatch.setattr(llm, "root_client", SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "root_async_client", SimpleNamespace(responses=SimpleNamespace(create=async_create)))
    return llm, create, async_create


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("wire_format", ["done_only", "interleaved_deltas"])
def test_parallel_calls_keep_arguments_and_usage(monkeypatch, asynchronous, wire_format, event_loop):
    calls = [
        ("get_macro_indicators", {"indicator": "cpi", "curr_date": "2026-11-08"}),
        ("get_macro_indicators", {"indicator": "unemployment", "curr_date": "2026-11-08"}),
        ("get_prediction_markets", {"topic": "Fed rate cut", "limit": 6}),
    ]
    complete = [ResponseFunctionToolCall(
        type="function_call", id=f"fc_{i}", call_id=f"call_{i}",
        name=name, arguments=json.dumps(args), status="completed",
    ) for i, (name, args) in enumerate(calls)]
    # The observed subscription stream sent arguments.done without deltas.
    # Also cover all names arriving before interleaved argument deltas.
    events = [SimpleNamespace(type="response.output_item.added", output_index=i,
                              item=item.model_copy(update={"arguments": ""}))
              for i, item in enumerate(complete)]
    event_type = ("response.function_call_arguments.done" if wire_format == "done_only"
                  else "response.function_call_arguments.delta")
    events += [SimpleNamespace(type=event_type, output_index=i,
                               item_id=item.id, delta=item.arguments, arguments=item.arguments)
               for i, item in enumerate(complete)]
    events.append(SimpleNamespace(type="response.completed", response=response(complete)))
    llm, create, async_create = client(monkeypatch, events)
    bound = llm.bind_tools([get_macro_indicators, get_prediction_markets])
    result = event_loop.run_until_complete(bound.ainvoke([HumanMessage("Macro context")])) if asynchronous else bound.invoke([HumanMessage("Macro context")])
    assert [(c["name"], c["args"]) for c in result.tool_calls] == calls
    assert [c["id"] for c in result.tool_calls] == ["call_0", "call_1", "call_2"]
    assert result.usage_metadata["total_tokens"] == 15
    payload = (async_create if asynchronous else create).call_args.kwargs
    assert payload["stream"] is True and payload["store"] is False


@pytest.mark.parametrize("terminal", [None, "response.failed", "response.incomplete", "error"])
def test_unfinished_or_failed_stream_never_returns_a_tool_call(monkeypatch, terminal):
    events = []
    if terminal == "error":
        events.append(SimpleNamespace(type=terminal, code="limit", message="usage limit"))
    elif terminal:
        events.append(SimpleNamespace(type=terminal, response=SimpleNamespace(
            error="usage limit", incomplete_details="incomplete")))
    llm, _, _ = client(monkeypatch, events)
    with pytest.raises(ValueError, match="ChatGPT"):
        llm.invoke([HumanMessage("Macro context")])


@pytest.mark.parametrize("method", ["function_calling", "json_schema"])
def test_completed_structured_decision_still_parses(monkeypatch, method):
    decision = {"rating": "Hold", "executive_summary": "Wait.", "investment_thesis": "Balanced.",
                "price_target": None, "time_horizon": None}
    item = ResponseOutputMessage(type="message", id="msg_test", role="assistant", status="completed",
                                 content=[{"type": "output_text", "text": json.dumps(decision), "annotations": []}])
    if method == "function_calling":
        item = ResponseFunctionToolCall(type="function_call", id="fc_decision", call_id="call_decision",
                                        name="PortfolioDecision", arguments=json.dumps(decision), status="completed")
    llm, _, _ = client(monkeypatch, [SimpleNamespace(type="response.completed", response=response([item]))])
    result = llm.with_structured_output(PortfolioDecision, method=method).invoke([HumanMessage("Decide")])
    assert result.rating.value == "Hold"
    assert result.executive_summary == "Wait."
