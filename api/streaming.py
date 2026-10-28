"""
Server-sent events for the streamed replies of /v1: the chunks of one reply in
OpenAI's envelope, a streamed inference begun so that a model that fails to
load still answers with its status, and the event lines of the reply as the
model writes it. Nothing here knows the shape of a chat or completion choice;
the routes pass those in.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Dict

from config import Config
from errors import describe
from models import ModelManager
from models.chat import ReplyStream

logger = logging.getLogger(__name__)

DONE = object()


def sse(body: Dict[str, Any]) -> str:
    return f"data: {json.dumps(body)}\n\n"


class Chunks:
    """The SSE chunks of one streamed reply, each in OpenAI's envelope (id, object, created, model)."""

    def __init__(self, id: str, object: str, created: int, model: str, include_usage: bool):
        self.envelope = {"id": id, "object": object, "created": created, "model": model}
        self.include_usage = include_usage

    def event(self, choice: Dict[str, Any], **extra: Any) -> str:
        return sse({**self.envelope, "choices": [{"index": 0, **choice}], **extra})

    def closing(self, choice: Dict[str, Any], fields: Dict[str, Any]):
        """The last choice with the reply's closing ``fields`` (toolkit, usage); with include_usage, OpenAI's usage
        chunk after it."""
        yield self.event(choice, **fields)
        if self.include_usage and "usage" in fields:
            # OpenAI's shape: a last chunk with no choices and the usage.
            yield sse({**self.envelope, "choices": [], "usage": fields["usage"]})


async def begin_stream(manager: ModelManager, model: str, task: str, task_input: Dict[str, Any]):
    """Start a streamed inference; returns (pieces, running, stop, first).

    The model hands over text from its worker thread; the ``pieces`` queue
    brings it back to the event loop, ending with DONE. Setting ``stop``
    makes the model stop generating. The first piece is awaited here, so a
    model that fails to load still gets a proper error status instead of a
    broken stream.
    """
    loop = asyncio.get_running_loop()
    pieces: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def on_text(piece: str) -> bool:
        loop.call_soon_threadsafe(pieces.put_nowait, piece)
        return not stop.is_set()

    def finished(task: asyncio.Task) -> None:
        if not task.cancelled():
            # Mark a failure as seen: it is reported below, or to monitoring if the client left.
            task.exception()
        # Queued after every piece, since the thread's last call_soon_threadsafe precedes the task's end.
        pieces.put_nowait(DONE)

    running = asyncio.create_task(manager.run_inference(model, task, task_input, on_text=on_text))
    running.add_done_callback(finished)
    first = await pieces.get()
    if first is DONE:
        # Raises what the model did: a refusal, or an error api/app.py answers as a 500.
        await running
    return pieces, running, stop, first


async def stream_events(started, reply: ReplyStream, text_chunk, closing):
    """The SSE lines of a streamed reply: ``text_chunk(delta)`` per piece, then ``closing(output)``'s.

    A failure mid-reply is an error event; either way the stream ends with [DONE].
    """
    pieces, running, stop, item = started
    streamed = False
    try:
        while item is not DONE:
            streamed = True
            delta = reply.feed(item)
            if delta:
                yield text_chunk(delta)
            if reply.ended:
                stop.set()
            item = await pieces.get()
        try:
            output = running.result()
        except Exception as e:
            # The status has gone out: the failure is reported in the stream, as api/app.py would have.
            logger.exception("Streamed inference failed")
            message = describe(e, "Internal error", Config.DEBUG)
            yield sse({"error": {"message": message, "type": "server_error"}})
            yield "data: [DONE]\n\n"
            return
        # A model that does not stream answers all at once.
        rest = reply.finish() if streamed else reply.feed(output["result"].text) + reply.finish()
        if rest:
            yield text_chunk(rest)
        for line in closing(output):
            yield line
        yield "data: [DONE]\n\n"
    finally:
        # Also reached when the client goes away mid-reply: stop generating.
        stop.set()
