"""ASGI admission through the full native handler and its sync-thread cleanup."""
import coordination as gate

# Native settings/setup can touch stores. A replacement web worker cannot
# initialize during a checkpoint. This also fences Uvicorn's worker respawns.
with gate.writer():
    from config.asgi import application as native_application


async def application(scope, receive, send):
    await gate.admitted(native_application, scope, receive, send)
