import asyncio
from contextlib import asynccontextmanager

from aiorpcx import TaskGroup


async def _check_results(tasks):
    async for task in tasks:
        if not task.cancelled():
            task.result()


async def _cancel_and_check(group):
    await group.cancel_remaining()
    await _check_results(group)


async def _drain_workers(group):
    drain = asyncio.create_task(_cancel_and_check(group))
    interrupted = None
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError as exc:
            interrupted = exc
    drain.result()
    return interrupted


async def cancel_and_join(group):
    """Drain a group not owned by a supervisor and observe any worker failures."""
    try:
        interrupted = await _drain_workers(group)
    finally:
        await group.join()
    if interrupted is not None:
        raise interrupted


@asynccontextmanager
async def task_group(group=None, *, on_closing=None):
    """Propagate worker errors; aiorpcX join() alone only cancels siblings."""
    async with (group if group is not None else TaskGroup()) as tasks:
        try:
            try:
                yield tasks
                await _check_results(tasks)
            finally:
                # Close admission before scheduling cancellation of existing workers.
                if on_closing is not None:
                    on_closing()
        except BaseException:
            await _drain_workers(tasks)
            raise
