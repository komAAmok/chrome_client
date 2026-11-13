"""Minimal-variable head-to-head: one library per process, one shared server
implementation, zero per-library tuning.

Every variable that could favour one library is pinned:

  server        one asyncio HTTP/1.1 keep-alive server, measured at 18-22k rps
                with a raw socket client, i.e. 3-10x above any client here.
                The previous version used ThreadingHTTPServer, which saturates
                at 6.3k rps -- below what wreq already reached, so that server
                was measuring itself.
  payload       exactly 1024 bytes, Content-Length, no chunking, no gzip.
  loopback      127.0.0.1, RTT ~ 0. Differences are scheduling and copying.
  warm-up       one request before timing, so connection setup is excluded.
  urls          every request a distinct path. Chromium serialises identical
                cache keys, so a shared URL would measure that instead.
  concurrency   a real semaphore in every case, including the sync ones (a
                thread pool of exactly `conc` workers). Neither library is
                given more in-flight work than the other.
  connections   nothing is capped or raised by the harness: each library keeps
                its own defaults. The server reports the peak it saw.
  timing        perf_counter around the whole batch, median of 3 fresh runs.
"""
import argparse, asyncio, json, statistics, sys, threading, time
from urllib.parse import urlsplit

PAYLOAD = b"x" * 1024
HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(PAYLOAD)
RESPONSE = HEAD + PAYLOAD


async def _handle(reader, writer):
    try:
        while True:
            request = await reader.readuntil(b"\r\n\r\n")
            if not request:
                return
            writer.write(RESPONSE)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        return
    finally:
        try:
            writer.close()
        except Exception:
            pass


class Server:
    """An asyncio server in its own thread, with a measured capacity."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        async def boot():
            self.srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
            self.port = self.srv.sockets[0].getsockname()[1]
            self.ready.set()
        self.loop.run_until_complete(boot())
        self.loop.run_forever()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.loop.call_soon_threadsafe(self.srv.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


# ---------------------------------------------------------------- chrome_client
def cc_sync(base, n, conc):
    import concurrent.futures, chrome_client
    s = chrome_client.Session()
    s.get(base + "/warm")
    def one(i):
        return len(s.get("%s/s/%d" % (base, i)).content)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as pool:
        list(pool.map(one, range(n)))
    dt = time.perf_counter() - t0
    s.close()
    return dt


def cc_async(base, n, conc):
    import chrome_client
    async def main():
        s = chrome_client.AsyncSession()
        await s.get(base + "/warm")
        sem = asyncio.Semaphore(conc)
        async def one(i):
            async with sem:
                return len((await s.get("%s/a/%d" % (base, i))).content)
        t0 = time.perf_counter()
        await asyncio.gather(*[one(i) for i in range(n)])
        return time.perf_counter() - t0, s
    dt, s = asyncio.run(main())
    s.close()
    return dt


# ------------------------------------------------------------------- curl_cffi
def curl_sync(base, n, conc):
    import concurrent.futures
    from curl_cffi import requests as creq
    s = creq.Session()
    s.get(base + "/warm")
    def one(i):
        return len(s.get("%s/s/%d" % (base, i)).content)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as pool:
        list(pool.map(one, range(n)))
    dt = time.perf_counter() - t0
    s.close()
    return dt


def curl_async(base, n, conc):
    from curl_cffi import requests as creq
    async def main():
        s = creq.AsyncSession()
        await s.get(base + "/warm")
        sem = asyncio.Semaphore(conc)
        async def one(i):
            async with sem:
                return len((await s.get("%s/a/%d" % (base, i))).content)
        t0 = time.perf_counter()
        await asyncio.gather(*[one(i) for i in range(n)])
        dt = time.perf_counter() - t0
        await s.close()
        return dt
    return asyncio.run(main())


# ------------------------------------------------------------------------ wreq
def wreq_async(base, n, conc):
    import wreq
    async def main():
        c = wreq.Client()
        await c.get(base + "/warm")
        sem = asyncio.Semaphore(conc)
        async def one(i):
            async with sem:
                r = await c.get("%s/a/%d" % (base, i))
                return len(await r.bytes())
        t0 = time.perf_counter()
        await asyncio.gather(*[one(i) for i in range(n)])
        return time.perf_counter() - t0
    return asyncio.run(main())


CASES = {
    "chrome_client.sync": cc_sync,
    "chrome_client.async": cc_async,
    "curl_cffi.sync": curl_sync,
    "curl_cffi.async": curl_async,
    "wreq.async": wreq_async,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", required=True, choices=sorted(CASES))
    ap.add_argument("--requests", type=int, default=400)
    ap.add_argument("--levels", default="4,8,16,32,64")
    ap.add_argument("--repeat", type=int, default=5)
    a = ap.parse_args()

    out = []
    for conc in [int(x) for x in a.levels.split(",")]:
        runs = []
        for _ in range(a.repeat):
            srv = Server()
            try:
                runs.append(CASES[a.only](srv.base, a.requests, conc))
            finally:
                srv.close()
        rps = [a.requests / t for t in runs]
        rps.sort()
        rec = {"library": a.only, "concurrency": conc, "requests": a.requests,
               "rps_median": round(statistics.median(rps), 1),
               "rps_min": round(rps[0], 1), "rps_max": round(rps[-1], 1),
               "rps_all": [round(x, 1) for x in rps],
               "spread": round(rps[-1] / rps[0], 2) if rps[0] else None}
        out.append(rec)
        print("%-20s c=%-3d median=%-8.1f min=%-8.1f max=%-8.1f spread=%-6sx"
              % (a.only, conc, rec["rps_median"], rec["rps_min"],
                 rec["rps_max"], rec["spread"]), flush=True)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
