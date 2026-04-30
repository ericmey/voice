"""
LangSmith span processor for LiveKit Agents.

Vendored from the LangSmith voice-agents-tracing demo repo:
https://github.com/langchain-ai/voice-agents-tracing/blob/main/livekit/langsmith_processor.py

Enriches OpenTelemetry spans from LiveKit Agents with LangSmith-compatible
attributes for proper conversation tracking and visualization.

LOCAL DELTA from upstream (search for "LOCAL DELTA" markers):
  1. ``function_tool`` span branch — extracts ``lk.function_tool.{name,arguments,output,is_error}``
     so tool calls render in LangSmith. Upstream's elif chain misses
     this span name and falls through to a content-less "chain".
  2. ``user_turn`` span branch — extracts ``lk.user_transcript`` so
     the user's speech-to-text transcription renders. Upstream's "stt"
     branch checks ``transcript`` / ``text`` / ``output`` attributes
     but LiveKit's user_turn span uses ``lk.user_transcript`` and a
     name that doesn't match upstream's ``stt`` heuristic.

Re-syncing from upstream: re-apply the two LOCAL DELTA blocks against
the new file. Upstream is largely dormant (single "rename" commit
since vendor) so churn risk is low.

The aggressive ``print(..., file=sys.stderr)`` calls are upstream's
design — they emit diagnostic chatter to stderr on every span.
Acceptable for first iteration; quiet later if needed.
"""
# ruff: noqa: T201, E501

import json
import logging
import os
from copy import deepcopy
from typing import Optional

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Optional verbose logging for local debugging
DEBUG = os.getenv("LANGSMITH_PROCESSOR_DEBUG", "false").lower() in ("true", "1", "yes")
logger = logging.getLogger("langsmith_processor")
if DEBUG and not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [LANGSMITH] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


class LangSmithSpanProcessor(SpanProcessor):
    """
    Custom OpenTelemetry span processor that enriches LiveKit Agents spans with LangSmith-compatible attributes.
    This enables proper conversation tracking and message visualization in LangSmith's UI.
    """

    def __init__(self, downstream_processor: Optional[SpanProcessor] = None):
        super().__init__()
        if downstream_processor is None:
            downstream_processor = BatchSpanProcessor(OTLPSpanExporter())
        self.downstream = downstream_processor
        # Track conversation messages across spans for proper LangSmith grouping
        self.conversation_messages = {}  # trace_id -> list of messages
        self.trace_to_conversation_id = {}  # trace_id -> conversation_id
        # Hold root/job spans until conversation data is ready
        self.deferred_job_spans = {}  # trace_id -> ReadableSpan

    def on_start(self, span: ReadableSpan, parent_context=None) -> None:
        if self.downstream:
            self.downstream.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """
        Enriches spans with LangSmith-compatible attributes before they're exported.
        Maps LiveKit Agents span types to LangSmith's expected format.
        """
        # Always log that we're processing a span (even without DEBUG mode)
        # Use print to stderr to ensure it's visible
        import sys
        print(f"[LANGSMITH-PROCESSOR] Processing span: {span.name}", file=sys.stderr, flush=True)

        # Track each conversation as a thread in LangSmith
        trace_id = format(span.context.trace_id, "032x")
        span._attributes["langsmith.metadata.thread_id"] = trace_id

        # Link all spans to their conversation for proper grouping in LangSmith
        if trace_id in self.trace_to_conversation_id:
            conversation_id = self.trace_to_conversation_id[trace_id]
            span._attributes["conversation.id"] = conversation_id
            span._attributes["langsmith.parent_span_id"] = "conversation"

        span_name = span.name.lower()

        # STT span: audio input -> transcribed text
        if "stt" in span_name or "speech_to_text" in span_name or "transcription" in span_name:
            span._attributes["langsmith.span.kind"] = "llm"
            transcript = span.attributes.get("transcript") or span.attributes.get("text") or span.attributes.get("output", "")
            self._set_prompt_attributes(span, [{"role": "user", "content": "audio_segment"}])
            if transcript:
                self._set_completion_attributes(span, [{"role": "assistant", "content": str(transcript)}])

        # LLM span: conversation messages -> AI response
        elif "llm" in span_name or "chat" in span_name or "completion" in span_name or "openai" in span_name:
            span._attributes["langsmith.span.kind"] = "llm"
            messages = self._extract_llm_messages(span)
            if not messages:
                messages = self._fallback_messages(span, span_name)
            self._set_prompt_attributes(span, messages)

            output_data = self._extract_llm_output(span)
            if output_data:
                completion = [{"role": "assistant", "content": str(output_data)}]
                self._set_completion_attributes(span, completion)
                self._track_messages(self.conversation_messages, trace_id, messages, str(output_data))

        # TTS span: text -> audio
        elif "tts" in span_name or "text_to_speech" in span_name or "synthesis" in span_name:
            span._attributes["langsmith.span.kind"] = "llm"

            # Debug TTS spans - always print attributes to see what LiveKit uses
            import sys
            print(f"\n[LANGSMITH-PROCESSOR] 🔊 TTS SPAN: {span.name}", file=sys.stderr, flush=True)
            print(f"  📋 All attributes for {span.name} ({len(span.attributes)} total):", file=sys.stderr, flush=True)
            for key, value in sorted(span.attributes.items()):
                value_str = str(value)
                if len(value_str) > 500:
                    value_str = value_str[:500] + "... (truncated)"
                print(f"    • {key} = {value_str}", file=sys.stderr, flush=True)

            # Try LiveKit-specific attributes first
            text = (
                span.attributes.get("lk.input_text")
                or span.attributes.get("lk.request.text")
                or span.attributes.get("lk.text")
                or span.attributes.get("text")
                or span.attributes.get("input")
                or span.attributes.get("prompt")
                or ""
            )

            # Extract voice/model from lk.tts_metrics or other attributes
            voice_id = "unknown"
            tts_metrics = span.attributes.get("lk.tts_metrics")
            if tts_metrics:
                try:
                    if isinstance(tts_metrics, str):
                        metrics_data = json.loads(tts_metrics)
                    else:
                        metrics_data = tts_metrics
                    if isinstance(metrics_data, dict):
                        metadata = metrics_data.get("metadata", {})
                        model_name = metadata.get("model_name") or metrics_data.get("model_name")
                        if model_name:
                            voice_id = str(model_name)
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

            # Fallback to other voice attributes
            if voice_id == "unknown":
                voice_id = (
                    span.attributes.get("lk.voice")
                    or span.attributes.get("voice")
                    or span.attributes.get("voice_id")
                    or "unknown"
                )

            print(f"  ✅ Extracted text: length={len(str(text))}, voice={voice_id}", file=sys.stderr, flush=True)

            self._set_prompt_attributes(
                span,
                [
                    {"role": "system", "content": f"Convert to speech with voice: {voice_id}"},
                    {"role": "user", "content": str(text) if text else "text_to_speech"},
                ],
            )
            self._set_completion_attributes(span, [{"role": "assistant", "content": f"Generated audio for: {text}"}])

        # Agent/Chain/Job spans: aggregate conversation
        elif (
            "agent" in span_name
            or "session" in span_name
            or "conversation" in span_name
            or "job" in span_name
        ):
            span._attributes["langsmith.span.kind"] = "chain"
            is_job_span = "job" in span_name

            # Try to extract conversation ID
            conversation_id = (
                span.attributes.get("conversation.id")
                or span.attributes.get("conversation_id")
                or span.attributes.get("session_id")
                or (span.attributes.get("lk.job_id") if is_job_span else "")
                or ""
            )
            if conversation_id:
                self.trace_to_conversation_id[trace_id] = str(conversation_id)
                span._attributes["conversation.id"] = str(conversation_id)
                span._attributes["langsmith.root_span"] = True
            elif is_job_span:
                # Ensure the root job span is treated as the LangSmith conversation root
                span._attributes["conversation.id"] = trace_id
                span._attributes["langsmith.root_span"] = True

            # Aggregate messages from conversation
            conv_msgs = self.conversation_messages.get(trace_id, [])
            if conv_msgs:
                system_msg, first_user_msg, remaining_msgs = self._split_conversation_messages(conv_msgs)

                # Add input (first user message only, exclude system message)
                # System message is only shown in LLM call spans, not in job entrypoint
                prompt_msgs = []
                if first_user_msg:
                    prompt_msgs.append(first_user_msg)
                if prompt_msgs:
                    self._set_prompt_attributes(span, prompt_msgs)

                # Add output (remaining conversation)
                if remaining_msgs:
                    self._set_completion_attributes(span, remaining_msgs)
                self._release_job_span_if_waiting(trace_id, prompt_msgs, remaining_msgs)
            elif is_job_span:
                # Defer export until conversation data becomes available
                self._defer_job_span(trace_id, span)
                return

            # Cleanup
            should_cleanup_trace = is_job_span or span.parent is None
            if should_cleanup_trace:
                if trace_id in self.conversation_messages:
                    del self.conversation_messages[trace_id]
                if trace_id in self.trace_to_conversation_id:
                    del self.trace_to_conversation_id[trace_id]

        # LOCAL DELTA #1: function_tool span — LiveKit emits one per tool
        # invocation with lk.function_tool.{id,name,arguments,output,is_error}
        # attributes. Upstream's elif chain misses this span name; without
        # this branch tool calls show up as empty "chain" spans.
        elif span_name == "function_tool" or "function_tool" in span_name:
            span._attributes["langsmith.span.kind"] = "tool"
            tool_name = str(span.attributes.get("lk.function_tool.name", "unknown_tool"))
            tool_args = span.attributes.get("lk.function_tool.arguments", "")
            tool_output = span.attributes.get("lk.function_tool.output", "")
            is_error = bool(span.attributes.get("lk.function_tool.is_error", False))

            # Render the tool call as a single-message exchange so the LangSmith
            # UI groups it as "called tool X with args Y, got Z".
            self._set_prompt_attributes(
                span,
                [{"role": "user", "content": f"call {tool_name}({tool_args})"}],
            )
            completion_role = "tool"
            completion_content = f"[error] {tool_output}" if is_error else str(tool_output)
            self._set_completion_attributes(
                span,
                [{"role": completion_role, "content": completion_content}],
            )

            # Track this tool call as an assistant turn in the conversation
            # aggregation so the conversation/job span shows the tool was used.
            self._track_messages(
                self.conversation_messages,
                trace_id,
                [{"role": "user", "content": f"called {tool_name}"}],
                f"{tool_name} → {completion_content}",
            )

        # LOCAL DELTA #2: user_turn span — LiveKit emits one per user
        # speech turn with lk.user_transcript holding the STT output.
        # Upstream's "stt" branch matches by name and checks `transcript`/`text`
        # but LiveKit uses `lk.user_transcript` here — different attribute name
        # AND the span is called `user_turn` not `stt`. Without this branch
        # we lose the user's actual words from every turn span.
        elif span_name == "user_turn" or "user_turn" in span_name:
            span._attributes["langsmith.span.kind"] = "chain"
            transcript = str(span.attributes.get("lk.user_transcript", "")) or ""
            confidence = span.attributes.get("lk.transcript_confidence", "")

            if transcript:
                # Surface the transcript as the user-turn's prompt so it
                # shows up in the LangSmith UI alongside the agent response.
                self._set_prompt_attributes(
                    span,
                    [{"role": "user", "content": transcript}],
                )
                self._set_completion_attributes(
                    span,
                    [{"role": "user", "content": transcript}],
                )
                # Push into the conversation aggregator so the parent
                # session span renders the user's actual words, not "Turn N".
                self._track_messages(
                    self.conversation_messages,
                    trace_id,
                    [{"role": "user", "content": transcript}],
                    "",
                )

                # Add confidence as a metadata attribute the LangSmith UI
                # can surface in the span sidebar.
                if confidence:
                    span._attributes["langsmith.metadata.transcript_confidence"] = str(confidence)

        # Default: mark as chain if no specific type detected
        else:
            # Check if it has LLM-like attributes
            if span.attributes.get("input") or span.attributes.get("output"):
                span._attributes["langsmith.span.kind"] = "llm"
                input_val = span.attributes.get("input", "")
                output_val = span.attributes.get("output", "")
                if input_val:
                    self._set_prompt_attributes(span, [{"role": "user", "content": str(input_val)}])
                if output_val:
                    self._set_completion_attributes(span, [{"role": "assistant", "content": str(output_val)}])
            else:
                span._attributes["langsmith.span.kind"] = "chain"

        # Export span downstream (unless it was deferred earlier)
        self._export_span(span)

    def _set_prompt_attributes(self, span: ReadableSpan, messages: list, start_idx: int = 0, log: bool = False):
        """Set gen_ai.prompt.* attributes from a list of messages."""
        import sys
        for i, msg in enumerate(messages):
            idx = start_idx + i
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                content = str(msg.get("content", ""))
                span._attributes[f"gen_ai.prompt.{idx}.role"] = role
                span._attributes[f"gen_ai.prompt.{idx}.content"] = content
                if log:
                    content_preview = content[:100] + "..." if len(content) > 100 else content
                    print(f"    Set gen_ai.prompt.{idx}.role = '{role}', gen_ai.prompt.{idx}.content = '{content_preview}' (length: {len(content)})", file=sys.stderr, flush=True)
            else:
                # Handle string messages
                content = str(msg)
                span._attributes[f"gen_ai.prompt.{idx}.role"] = "user"
                span._attributes[f"gen_ai.prompt.{idx}.content"] = content
                if log:
                    content_preview = content[:100] + "..." if len(content) > 100 else content
                    print(f"    Set gen_ai.prompt.{idx}.role = 'user', gen_ai.prompt.{idx}.content = '{content_preview}' (length: {len(content)})", file=sys.stderr, flush=True)

    def _set_completion_attributes(self, span: ReadableSpan, messages: list, start_idx: int = 0, log: bool = False):
        """Set gen_ai.completion.* attributes from a list of messages."""
        import sys
        for i, msg in enumerate(messages):
            idx = start_idx + i
            if isinstance(msg, dict):
                role = msg.get("role", "assistant")
                content = str(msg.get("content", ""))
                span._attributes[f"gen_ai.completion.{idx}.role"] = role
                span._attributes[f"gen_ai.completion.{idx}.content"] = content
                if log:
                    content_preview = content[:200] + "..." if len(content) > 200 else content
                    print(f"    Set gen_ai.completion.{idx}.role = '{role}', gen_ai.completion.{idx}.content = '{content_preview}' (length: {len(content)})", file=sys.stderr, flush=True)
            else:
                # Handle string messages
                content = str(msg)
                span._attributes[f"gen_ai.completion.{idx}.role"] = "assistant"
                span._attributes[f"gen_ai.completion.{idx}.content"] = content
                if log:
                    content_preview = content[:200] + "..." if len(content) > 200 else content
                    print(f"    Set gen_ai.completion.{idx}.role = 'assistant', gen_ai.completion.{idx}.content = '{content_preview}' (length: {len(content)})", file=sys.stderr, flush=True)

    def _fallback_messages(self, span: ReadableSpan, span_name: str) -> list:
        """Use system/user attributes or span name when no chat context is available."""
        system_prompt = span.attributes.get("gen_ai.system") or span.attributes.get("system") or ""
        user_prompt = (
            span.attributes.get("gen_ai.user")
            or span.attributes.get("user")
            or span.attributes.get("input")
            or ""
        )
        fallback = []
        if system_prompt:
            fallback.append({"role": "system", "content": str(system_prompt)})
        if user_prompt:
            fallback.append({"role": "user", "content": str(user_prompt)})
        if not fallback:
            fallback.append({"role": "user", "content": f"LLM request: {span_name}"})
        return fallback

    def _split_conversation_messages(self, messages: list) -> tuple:
        """
        Split conversation messages into system, first user, and remaining messages.
        Returns: (system_msg, first_user_msg, remaining_msgs)
        """
        system_msg = None
        first_user_msg = None
        remaining_msgs = []
        first_user_found = False

        for msg in messages:
            role = msg.get("role", "") if isinstance(msg, dict) else "user"
            if role == "system" and system_msg is None:
                system_msg = msg
            elif role == "user" and not first_user_found:
                first_user_msg = msg
                first_user_found = True
            elif first_user_found:
                remaining_msgs.append(msg)

        return (system_msg, first_user_msg, remaining_msgs)

    def _extract_llm_messages(self, span: ReadableSpan) -> list:
        """
        Extract LLM input messages from span attributes using multiple strategies.
        Returns a list of message dicts with 'role' and 'content' keys.
        """
        import sys
        print("  🔍 Strategy 1: Checking lk.chat_ctx...", file=sys.stderr, flush=True)

        # Strategy 1: LiveKit-specific attribute: lk.chat_ctx
        chat_ctx = span.attributes.get("lk.chat_ctx")
        if chat_ctx:
            print(f"    ✓ Found lk.chat_ctx, type={type(chat_ctx)}, length={len(str(chat_ctx)) if isinstance(chat_ctx, str) else 'N/A'}", file=sys.stderr, flush=True)
            try:
                if isinstance(chat_ctx, str):
                    ctx_data = json.loads(chat_ctx)
                else:
                    ctx_data = chat_ctx

                # Extract messages from items array
                if isinstance(ctx_data, dict) and "items" in ctx_data:
                    messages = []
                    for item in ctx_data["items"]:
                        if isinstance(item, dict) and item.get("type") == "message":
                            role = item.get("role", "user")
                            content = item.get("content", "")
                            # Content might be a list of strings or a single string
                            if isinstance(content, list):
                                content = " ".join(str(c) for c in content)
                            if content:
                                messages.append({"role": str(role), "content": str(content)})

                    if messages:
                        print(f"    ✅ Strategy 1 SUCCESS: Found {len(messages)} messages from lk.chat_ctx", file=sys.stderr, flush=True)
                        return messages
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as e:
                print(f"    ✗ Strategy 1 FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        else:
            print("    ✗ lk.chat_ctx not found", file=sys.stderr, flush=True)

        # Strategy 2: Check for OpenTelemetry semantic convention attributes
        # gen_ai.request.prompt.* or gen_ai.prompt.*
        print("  🔍 Strategy 2: Checking gen_ai.request.prompt.*...", file=sys.stderr, flush=True)
        messages = []
        idx = 0
        while True:
            role_key = f"gen_ai.request.prompt.{idx}.role"
            content_key = f"gen_ai.request.prompt.{idx}.content"
            if role_key in span.attributes or content_key in span.attributes:
                role = span.attributes.get(role_key, "user")
                content = span.attributes.get(content_key, "")
                if content:
                    messages.append({"role": str(role), "content": str(content)})
                idx += 1
            else:
                break

        if messages:
            print(f"    ✅ Strategy 2 SUCCESS: Found {len(messages)} messages from gen_ai.request.prompt.*", file=sys.stderr, flush=True)
            return messages
        else:
            print("    ✗ No gen_ai.request.prompt.* attributes found", file=sys.stderr, flush=True)

        # Strategy 2b: Check for gen_ai.prompt.* (alternative format)
        print("  🔍 Strategy 2b: Checking gen_ai.prompt.*...", file=sys.stderr, flush=True)
        idx = 0
        while True:
            role_key = f"gen_ai.prompt.{idx}.role"
            content_key = f"gen_ai.prompt.{idx}.content"
            if role_key in span.attributes or content_key in span.attributes:
                role = span.attributes.get(role_key, "user")
                content = span.attributes.get(content_key, "")
                if content:
                    messages.append({"role": str(role), "content": str(content)})
                idx += 1
            else:
                break

        if messages:
            print(f"    ✅ Strategy 2b SUCCESS: Found {len(messages)} messages from gen_ai.prompt.*", file=sys.stderr, flush=True)
            return messages
        else:
            print("    ✗ No gen_ai.prompt.* attributes found", file=sys.stderr, flush=True)

        # Strategy 3: Check for messages attribute (JSON string or list)
        print("  🔍 Strategy 3: Checking messages/llm.messages/input attributes...", file=sys.stderr, flush=True)
        messages_attr = span.attributes.get("messages") or span.attributes.get("llm.messages") or span.attributes.get("input")
        print(f"    Checking: messages={bool(span.attributes.get('messages'))}, llm.messages={bool(span.attributes.get('llm.messages'))}, input={bool(span.attributes.get('input'))}", file=sys.stderr, flush=True)
        if messages_attr:
            try:
                if isinstance(messages_attr, str):
                    if DEBUG:
                        logger.debug(f"  Parsing JSON string, length={len(messages_attr)}")
                    parsed = json.loads(messages_attr)
                    if isinstance(parsed, list):
                        # Validate and normalize message format
                        normalized = []
                        for msg in parsed:
                            if isinstance(msg, dict) and "content" in msg:
                                normalized.append({
                                    "role": msg.get("role", "user"),
                                    "content": str(msg.get("content", "")),
                                })
                        if normalized:
                            print(f"    ✅ Strategy 3 SUCCESS: Found {len(normalized)} messages from JSON string", file=sys.stderr, flush=True)
                            return normalized
                elif isinstance(messages_attr, list):
                    print(f"    Found list type, length={len(messages_attr)}", file=sys.stderr, flush=True)
                    # Validate and normalize message format
                    normalized = []
                    for msg in messages_attr:
                        if isinstance(msg, dict) and "content" in msg:
                            normalized.append({
                                "role": msg.get("role", "user"),
                                "content": str(msg.get("content", "")),
                            })
                    if normalized:
                        print(f"    ✅ Strategy 3 SUCCESS: Found {len(normalized)} messages from list", file=sys.stderr, flush=True)
                        return normalized
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                print(f"    ✗ Strategy 3 FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        else:
            print("    ✗ No messages attribute found", file=sys.stderr, flush=True)

        # Strategy 4: Check for individual system/user/assistant attributes
        print("  🔍 Strategy 4: Checking individual system/user/assistant attributes...", file=sys.stderr, flush=True)
        system = span.attributes.get("gen_ai.system") or span.attributes.get("system") or span.attributes.get("system_prompt")
        user = span.attributes.get("gen_ai.user") or span.attributes.get("user") or span.attributes.get("user_input")
        assistant = span.attributes.get("gen_ai.assistant") or span.attributes.get("assistant")

        print(f"    system={bool(system)}, user={bool(user)}, assistant={bool(assistant)}", file=sys.stderr, flush=True)

        if system or user or assistant:
            result = []
            if system:
                result.append({"role": "system", "content": str(system)})
            if user:
                result.append({"role": "user", "content": str(user)})
            if assistant:
                result.append({"role": "assistant", "content": str(assistant)})
            if result:
                print(f"    ✅ Strategy 4 SUCCESS: Found {len(result)} messages from individual attributes", file=sys.stderr, flush=True)
                return result
        else:
            print("    ✗ No individual attributes found", file=sys.stderr, flush=True)

        print("  ⚠️  All strategies failed - no messages extracted", file=sys.stderr, flush=True)
        return []

    def _extract_llm_output(self, span: ReadableSpan) -> str:
        """
        Extract LLM output/completion from span attributes using multiple strategies.
        Returns the output as a string.
        """
        import sys
        print("  🔍 EXTRACTING LLM OUTPUT:", file=sys.stderr, flush=True)

        # Strategy 1: LiveKit-specific attribute: lk.response.text
        print("    Strategy 1: Checking lk.response.text...", file=sys.stderr, flush=True)
        output = span.attributes.get("lk.response.text")
        if output:
            print(f"      ✅ Strategy 1 SUCCESS: Found output, length={len(str(output))}", file=sys.stderr, flush=True)
            return str(output)
        else:
            print("      ✗ lk.response.text not found", file=sys.stderr, flush=True)

        # Strategy 2: OpenTelemetry semantic convention
        print("    Strategy 2: Checking gen_ai.response.text / gen_ai.completion.text...", file=sys.stderr, flush=True)
        output = span.attributes.get("gen_ai.response.text") or span.attributes.get("gen_ai.completion.text")
        if output:
            print(f"      ✅ Strategy 2 SUCCESS: Found output, length={len(str(output))}", file=sys.stderr, flush=True)
            return str(output)
        else:
            print("      ✗ gen_ai.response.text and gen_ai.completion.text not found", file=sys.stderr, flush=True)

        # Strategy 3: Common attribute names
        print("    Strategy 3: Checking common attribute names...", file=sys.stderr, flush=True)
        output = (
            span.attributes.get("gen_ai.response")
            or span.attributes.get("gen_ai.completion")
            or span.attributes.get("output")
            or span.attributes.get("response")
            or span.attributes.get("completion")
            or span.attributes.get("llm.output")
            or span.attributes.get("llm.response")
            or span.attributes.get("text")
            or ""
        )

        if output:
            print(f"      ✅ Strategy 3 SUCCESS: Found output, length={len(str(output))}", file=sys.stderr, flush=True)
            return str(output)
        else:
            print("      ✗ No common output attributes found", file=sys.stderr, flush=True)

        # Strategy 4: Check for completion.* attributes
        print("    Strategy 4: Checking gen_ai.completion.* attributes...", file=sys.stderr, flush=True)
        idx = 0
        completion_parts = []
        while True:
            content_key = f"gen_ai.completion.{idx}.content"
            if content_key in span.attributes:
                completion_parts.append(str(span.attributes[content_key]))
                idx += 1
            else:
                break

        if completion_parts:
            print(f"      ✅ Strategy 4 SUCCESS: Found {len(completion_parts)} completion parts", file=sys.stderr, flush=True)
            return "\n".join(completion_parts)
        else:
            print("      ✗ No gen_ai.completion.* attributes found", file=sys.stderr, flush=True)

        print("    ⚠️  All strategies failed - no output extracted", file=sys.stderr, flush=True)
        return ""

    def _get_messages_from_attributes(self, span: ReadableSpan) -> list:
        """Extract messages from span attributes as fallback."""
        messages = []
        system = span.attributes.get("gen_ai.system") or span.attributes.get("system")
        user = span.attributes.get("gen_ai.user") or span.attributes.get("user") or span.attributes.get("input")

        if system:
            messages.append({"role": "system", "content": str(system)})
        if user:
            messages.append({"role": "user", "content": str(user)})

        return messages

    def _track_messages(self, target_dict: dict, key: str, messages: list, output_data: str):
        """
        Track messages in target_dict, avoiding duplicates.
        Preserves deduplication logic: case-insensitive content comparison.
        """
        if key not in target_dict:
            target_dict[key] = []
            # Add system prompt once at the start
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    target_dict[key].append(msg)
                    break

        # Add the latest user message if it's new
        last_user_msg = next(
            (msg for msg in reversed(messages) if isinstance(msg, dict) and msg.get("role") == "user"),
            None,
        )
        if last_user_msg:
            new_content = str(last_user_msg.get("content", "")).strip().lower()
            existing_contents = [
                str(m.get("content", "")).strip().lower()
                for m in target_dict[key]
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            if new_content and new_content not in existing_contents:
                target_dict[key].append(last_user_msg)

        # Add the assistant response if it's new
        if output_data:
            new_assistant_content = str(output_data).strip().lower()
            existing_assistant_contents = [
                str(m.get("content", "")).strip().lower()
                for m in target_dict[key]
                if isinstance(m, dict) and m.get("role") == "assistant"
            ]
            if new_assistant_content not in existing_assistant_contents:
                target_dict[key].append({"role": "assistant", "content": output_data})

    def shutdown(self) -> None:
        self._flush_deferred_job_spans()
        if self.downstream:
            self.downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self._flush_deferred_job_spans()
        if self.downstream:
            return self.downstream.force_flush(timeout_millis)
        return True

    def _defer_job_span(self, trace_id: str, span: ReadableSpan):
        import sys
        self.deferred_job_spans[trace_id] = span
        print(f"⏸️  Deferring export of job span for trace {trace_id}", file=sys.stderr, flush=True)

    def _release_job_span_if_waiting(self, trace_id: str, prompt_msgs: list, completion_msgs: list):
        job_span = self.deferred_job_spans.pop(trace_id, None)
        if not job_span:
            return
        import sys
        print(f"🧩 Releasing deferred job span for trace {trace_id}", file=sys.stderr, flush=True)
        if prompt_msgs:
            self._set_prompt_attributes(job_span, deepcopy(prompt_msgs))
        if completion_msgs:
            self._set_completion_attributes(job_span, deepcopy(completion_msgs))
        self._export_span(job_span)

    def _flush_deferred_job_spans(self):
        if not self.deferred_job_spans:
            return
        import sys
        print(f"⚠️  Flushing {len(self.deferred_job_spans)} deferred job span(s) without conversation data", file=sys.stderr, flush=True)
        for trace_id, span in list(self.deferred_job_spans.items()):
            self._set_prompt_attributes(span, [{"role": "system", "content": "Conversation not captured"}])
            self._set_completion_attributes(span, [{"role": "assistant", "content": "No conversation turns recorded."}])
            self._export_span(span)
            del self.deferred_job_spans[trace_id]

    def _export_span(self, span: ReadableSpan):
        if self.downstream:
            self.downstream.on_end(span)
