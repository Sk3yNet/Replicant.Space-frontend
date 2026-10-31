import asyncio
import types

from rsweb import pagecache


def _req():
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))


def test_stale_value_served_while_one_refresh_runs():
    async def go():
        req, calls = _req(), []

        async def fn():
            calls.append(1)
            await asyncio.sleep(0.01)
            return len(calls)
        assert await asyncio.gather(*(pagecache.get(req, "k", fn) for _ in range(3))) == [1, 1, 1]   # shared first run
        assert len(calls) == 1
        assert await pagecache.get(req, "k", fn, ttl=0) == 1   # stale: old value now, refresh in the background
        assert await pagecache.get(req, "k", fn, ttl=0) == 1   # same refresh, not a second one
        await asyncio.sleep(0.05)
        assert len(calls) == 2 and await pagecache.get(req, "k", fn) == 2
    asyncio.run(go())


def test_clear_drops_a_refresh_begun_before_it():
    async def go():
        req, n = _req(), [0]

        async def fn():
            n[0] += 1
            v = n[0]
            await asyncio.sleep(0.01)
            return v
        await pagecache.get(req, "k", fn)
        assert await pagecache.get(req, "k", fn, ttl=0) == 1   # refresh 2 starts
        pagecache.clear(req.app)
        assert await pagecache.get(req, "k", fn) == 3   # waits for a fresh run, not the pre-edit one
        await asyncio.sleep(0.05)
        assert await pagecache.get(req, "k", fn) == 3   # the pre-edit result wasn't kept
    asyncio.run(go())
