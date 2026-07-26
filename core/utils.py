import asyncio
import concurrent.futures
import contextvars


def run_async(coro):
    """Run an async coroutine from a synchronous context, safely."""
    try:
        asyncio.get_running_loop()
        # Loop is already running (e.g. in FastAPI/Eager mode).
        # Run in a separate thread to avoid 'nested loop' errors. Explicitly
        # carry the current context so safety gates implemented with
        # ContextVar (including the final-action LLM prohibition) cannot be
        # lost at this thread boundary.
        context = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(context.run, asyncio.run, coro)
            return future.result()
    except RuntimeError:
        # No loop running, use asyncio.run
        return asyncio.run(coro)
