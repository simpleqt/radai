"""Middleware for injecting image details into conversation before LLM call.

Uses local ViewedImageData type from core.agents.state.
"""

import logging
from typing_extensions import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from backend.core.agents.state import ViewedImageData

logger = logging.getLogger(__name__)


class ViewImageMiddlewareState(AgentState):
    viewed_images: NotRequired[dict[str, ViewedImageData] | None]


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """Injects image details as a human message before LLM calls."""

    state_schema = ViewImageMiddlewareState

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False
        return any(tc.get("name") == "view_image" for tc in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        if not hasattr(assistant_msg, "tool_calls") or not assistant_msg.tool_calls:
            return False
        tool_call_ids = {tc.get("id") for tc in assistant_msg.tool_calls if tc.get("id")}
        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False
        completed_tool_ids = set()
        for msg in messages[assistant_idx + 1:]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                completed_tool_ids.add(msg.tool_call_id)
        return tool_call_ids.issubset(completed_tool_ids)

    def _create_image_details_message(self, state: ViewImageMiddlewareState) -> list[str | dict]:
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            return [{"type": "text", "text": "No images have been viewed."}]

        content_blocks: list[str | dict] = [
            {"type": "text", "text": "Here are the images you've viewed:"}
        ]
        for image_path, image_data in viewed_images.items():
            mime_type = image_data.get("mime_type", "unknown")
            base64_data = image_data.get("base64", "")
            content_blocks.append(
                {"type": "text", "text": f"\n- **{image_path}** ({mime_type})"}
            )
            if base64_data:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
                })
        return content_blocks

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        messages = state.get("messages", [])
        if not messages:
            return False
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return False
        if not self._has_view_image_tool(last_assistant_msg):
            return False
        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1:]:
            if isinstance(msg, HumanMessage):
                content_str = str(msg.content)
                if "Here are the images you've viewed" in content_str:
                    return False
        return True

    def _inject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        if not self._should_inject_image_message(state):
            return None
        image_content = self._create_image_details_message(state)
        human_msg = HumanMessage(content=image_content)
        logger.debug("Injecting image details message with images before LLM call")
        return {"messages": [human_msg]}

    @override
    def before_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._inject_image_message(state)

    @override
    async def abefore_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._inject_image_message(state)
