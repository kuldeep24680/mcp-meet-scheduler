"""LLM adapters for the host. NO MCP code here.

The point: MCP tools are described with plain JSON Schema, so ANY tool-calling
LLM can use the same MCP server. Each adapter only does three translations:
  1. MCP tool list      -> provider's tool format
  2. provider response  -> (text, [ToolCall])
  3. tool results       -> provider's "tool result" message format

Conversation history lives inside the adapter (owned by the host, never the server).
Select with LLM_PROVIDER=openai (default) | anthropic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    call: ToolCall
    text: str
    is_error: bool


class OpenAIChat:
    """OpenAI Responses API (OpenAI's recommended API for tool calling)."""

    def __init__(self, model: str, system: str, mcp_tools):
        from openai import OpenAI  # reads OPENAI_API_KEY

        self.client = OpenAI()
        self.model = model
        self.system = system
        self.history: list = []
        # MCP Tool -> OpenAI function tool. `parameters` is the MCP inputSchema verbatim.
        # strict=False: strict mode demands additionalProperties=false + every field required,
        # which our schemas (optional args with defaults) intentionally don't satisfy.
        self.tools = [
            {"type": "function", "name": t.name, "description": t.description or "",
             "parameters": t.input_schema, "strict": False}
            for t in mcp_tools
        ]

    def checkpoint(self) -> int:
        return len(self.history)

    def restore(self, mark: int) -> None:
        del self.history[mark:]

    def add_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def complete(self) -> tuple[str, list[ToolCall]]:
        resp = self.client.responses.create(
            model=self.model, instructions=self.system, input=self.history, tools=self.tools,
        )
        # Keep the model's own items (incl. reasoning / function_call) in history, but drop
        # exact duplicate message items: gpt-5.4-mini intermittently emits the same message twice.
        texts, items = [], []
        for item in resp.output:
            if item.type == "message":
                text = "".join(getattr(c, "text", "") for c in item.content)
                if text in texts:
                    continue
                texts.append(text)
            items.append(item)
        self.history += items
        calls = [
            ToolCall(item.call_id, item.name, json.loads(item.arguments or "{}"))
            for item in items if item.type == "function_call"
        ]
        return "\n".join(t for t in texts if t), calls

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # OpenAI has no is_error flag: mark failures in the text so the model can adapt.
        for r in results:
            self.history.append({
                "type": "function_call_output", "call_id": r.call.id,
                "output": f"ERROR: {r.text}" if r.is_error else r.text,
            })


class AnthropicChat:
    """Anthropic Messages API."""

    def __init__(self, model: str, system: str, mcp_tools):
        from anthropic import Anthropic  # reads ANTHROPIC_API_KEY

        self.client = Anthropic()
        self.model = model
        self.system = system
        self.history: list[dict] = []
        # MCP Tool -> Claude tool: a 1:1 mapping, both are JSON Schema.
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in mcp_tools
        ]

    def checkpoint(self) -> int:
        return len(self.history)

    def restore(self, mark: int) -> None:
        del self.history[mark:]

    def add_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def complete(self) -> tuple[str, list[ToolCall]]:
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024, system=self.system, tools=self.tools, messages=self.history,
        )
        self.history.append({"role": "assistant", "content": resp.content})
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        return text, calls

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # Claude requires ALL results for one assistant turn in ONE user message, same order.
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call.id, "content": r.text, "is_error": r.is_error}
            for r in results
        ]})


PROVIDERS = {
    "openai": (OpenAIChat, "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-5.4-mini"),
    "anthropic": (AnthropicChat, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-sonnet-5-5"),
}


def resolve(provider: str, model: str | None) -> tuple[type, str]:
    """Validate provider + API key up front (before spawning the server); return (adapter class, model)."""
    if provider not in PROVIDERS:
        raise SystemExit(f"Unknown LLM_PROVIDER {provider!r}; use one of {list(PROVIDERS)}")
    cls, key_var, model_var, default_model = PROVIDERS[provider]
    if not os.getenv(key_var):
        raise SystemExit(f"Set {key_var} in .env (LLM_PROVIDER={provider})")
    return cls, model or os.getenv(model_var) or default_model
