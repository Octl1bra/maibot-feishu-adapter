"""给 k8s 探针用的最小 HTTP 端点。"""

from __future__ import annotations

from typing import Any, Callable

from aiohttp import web

StatusProvider = Callable[[], dict[str, Any]]


async def start_health_server(port: int, status: StatusProvider) -> web.AppRunner:
    async def healthz(_: web.Request) -> web.Response:
        snapshot = status()
        return web.json_response(snapshot, status=200 if snapshot.get("ok") else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
