"""Replay a buffered final Response using the public Responses SSE protocol.

The engine finishes all of its work before this module is called. These events
describe delivery of that final result; their timing is not upstream inference
timing. Unknown output kinds remain intact in item and terminal events. Only
public reasoning summaries receive derived events, never private reasoning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


_CHUNK_CHARACTERS = 512
_TERMINAL_EVENTS = {
    "completed": "response.completed",
    "incomplete": "response.incomplete",
    "failed": "response.failed",
}


def iter_response_events(response: dict[str, Any]) -> Iterator[bytes]:
    """Return SSE frames for an already finished, JSON-compatible Response.

    Snapshot and structural validation happen when this function is called,
    before its first data frame. A server may already have opened the stream for
    heartbeat comments. Invalid envelopes raise ValueError. Future item/content
    types and optional fields are preserved in the final objects without
    attempting to infer their intermediate states.

    Each frame is UTF-8 SSE with an event name and a JSON data object. Sequence
    numbers begin at zero. The final frame is the matching Response terminal
    event; there is no Chat Completions ``[DONE]`` marker. Usage is passed through
    exactly as supplied by the engine, including its aggregated stage costs.
    """
    if not isinstance(response, dict):
        raise ValueError("The final response must be a JSON object.")
    try:
        # The private snapshot prevents caller mutations during delivery. ASCII
        # JSON escaping also preserves strings containing isolated surrogates.
        final = json.loads(json.dumps(response, ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("The final response must contain valid JSON values.") from exc

    if not isinstance(final.get("id"), str) or not final["id"]:
        raise ValueError("The final response must have a nonempty string id.")
    status = final.get("status")
    if not isinstance(status, str) or status not in _TERMINAL_EVENTS:
        raise ValueError("The final response status must be completed, incomplete, or failed.")
    output = final.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ValueError("The final response output must be an array of objects.")

    return _encode_events(_response_events(final))


def _encode_events(events: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    for sequence_number, event in enumerate(events):
        event["sequence_number"] = sequence_number
        data = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        yield f"event: {event['type']}\ndata: {data}\n\n".encode("utf-8")


def _chunks(value: str) -> Iterator[str]:
    for offset in range(0, len(value), _CHUNK_CHARACTERS):
        yield value[offset : offset + _CHUNK_CHARACTERS]


def _response_events(final: dict[str, Any]) -> Iterator[dict[str, Any]]:
    opening = dict(final)
    opening["status"] = "in_progress"
    opening["output"] = []
    opening["usage"] = None
    opening["error"] = None
    opening["incomplete_details"] = None
    if "completed_at" in opening:
        opening["completed_at"] = None
    # Some compatible providers include this SDK convenience property on the
    # wire; it must not reveal the buffered answer in response.created.
    opening.pop("output_text", None)
    yield {"type": "response.created", "response": opening}
    yield {"type": "response.in_progress", "response": opening}

    for output_index, item in enumerate(final["output"]):
        item_type = item.get("type")
        item_id = item.get("id")
        has_id = isinstance(item_id, str) and bool(item_id)
        started = dict(item)

        if item_type == "message":
            started["content"] = []
        elif item_type == "function_call" and has_id and isinstance(item.get("arguments"), str):
            started["arguments"] = ""
        elif item_type == "reasoning":
            started["summary"] = []
            # Opaque/private fields are retained only on the complete item.
            started.pop("content", None)
            started.pop("encrypted_content", None)

        if item_type in ("message", "function_call", "reasoning") and "status" in started:
            started["status"] = "in_progress"

        yield {"type": "response.output_item.added", "output_index": output_index, "item": started}

        # Some final tool items legally omit id. Preserve those items without
        # inventing an identity for delta events, which require item_id.
        if has_id:
            location = {"item_id": item_id, "output_index": output_index}
            if item_type == "message":
                yield from _message_events(item, location)
            elif item_type == "function_call" and isinstance(item.get("arguments"), str):
                arguments = item["arguments"]
                for chunk in _chunks(arguments):
                    yield {"type": "response.function_call_arguments.delta", **location, "delta": chunk}
                yield {"type": "response.function_call_arguments.done", **location, "arguments": arguments}
            elif item_type == "reasoning":
                yield from _summary_events(item, location)

        yield {"type": "response.output_item.done", "output_index": output_index, "item": item}

    yield {"type": _TERMINAL_EVENTS[final["status"]], "response": final}


def _message_events(
    item: dict[str, Any], location: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    content = item.get("content")
    if not isinstance(content, list):
        return
    for content_index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "output_text" and isinstance(part.get("text"), str):
            yield from _text_events(part, {**location, "content_index": content_index})
        elif part_type == "refusal" and isinstance(part.get("refusal"), str):
            part_location = {**location, "content_index": content_index}
            started = {**part, "refusal": ""}
            yield {"type": "response.content_part.added", **part_location, "part": started}
            for chunk in _chunks(part["refusal"]):
                yield {"type": "response.refusal.delta", **part_location, "delta": chunk}
            yield {"type": "response.refusal.done", **part_location, "refusal": part["refusal"]}
            yield {"type": "response.content_part.done", **part_location, "part": part}
        # Other content stays on output_item.done and the terminal response.


def _text_events(
    part: dict[str, Any], location: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    started = {**part, "text": "", "annotations": [], "logprobs": []}
    yield {"type": "response.content_part.added", **location, "part": started}
    for chunk in _chunks(part["text"]):
        # Character chunks do not establish token boundaries. Actual supplied
        # logprobs are preserved in the done event and complete content part.
        yield {"type": "response.output_text.delta", **location, "delta": chunk, "logprobs": []}

    annotations = part.get("annotations")
    if isinstance(annotations, list):
        for annotation_index, annotation in enumerate(annotations):
            if annotation is not None and not isinstance(annotation, dict):
                continue
            yield {
                "type": "response.output_text.annotation.added",
                **location,
                "annotation_index": annotation_index,
                "annotation": annotation,
            }

    logprobs = part.get("logprobs")
    yield {
        "type": "response.output_text.done",
        **location,
        "text": part["text"],
        "logprobs": logprobs if isinstance(logprobs, list) else [],
    }
    yield {"type": "response.content_part.done", **location, "part": part}


def _summary_events(
    item: dict[str, Any], location: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return
    for summary_index, part in enumerate(summary):
        if not isinstance(part, dict) or part.get("type") != "summary_text" or not isinstance(part.get("text"), str):
            continue
        part_location = {**location, "summary_index": summary_index}
        yield {
            "type": "response.reasoning_summary_part.added",
            **part_location,
            "part": {**part, "text": ""},
        }
        for chunk in _chunks(part["text"]):
            yield {"type": "response.reasoning_summary_text.delta", **part_location, "delta": chunk}
        yield {"type": "response.reasoning_summary_text.done", **part_location, "text": part["text"]}
        yield {"type": "response.reasoning_summary_part.done", **part_location, "part": part}
