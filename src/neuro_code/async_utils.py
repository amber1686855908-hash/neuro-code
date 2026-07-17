from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


async def run_blocking[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run blocking work without occupying the event-loop thread.

    The short cooperative tick also makes completion reliable inside restricted
    process sandboxes where the executor's cross-thread wakeup descriptor may
    be delayed. On a normal event loop the task completes immediately and the
    tick does not affect the result or cancellation semantics.
    """

    completed = threading.Event()
    result: list[R] = []
    failure: list[BaseException] = []

    def invoke() -> None:
        try:
            result.append(function(*args, **kwargs))
        except BaseException as error:
            failure.append(error)
        finally:
            completed.set()

    # A daemon worker avoids coupling loop shutdown to the default executor.
    # Cancellation cannot stop synchronous OS work, so callers must keep each
    # operation bounded and idempotent just as they would with `to_thread`.
    threading.Thread(target=invoke, name="neuro-code-blocking", daemon=True).start()
    while not completed.is_set():  # noqa: ASYNC110 - threading.Event has no async wait
        await asyncio.sleep(0.01)
    if failure:
        raise failure[0]
    return result[0]
