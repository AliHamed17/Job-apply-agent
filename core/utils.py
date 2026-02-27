import asyncio
import concurrent.futures

def run_async(coro):
    """Run an async coroutine from a synchronous context, safely."""
    try:
        asyncio.get_running_loop()
        # Loop is already running (e.g. in FastAPI/Eager mode).
        # Run in a separate thread to avoid 'nested loop' errors.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    except RuntimeError:
        # No loop running, use asyncio.run
        return asyncio.run(coro)
