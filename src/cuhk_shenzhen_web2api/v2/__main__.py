"""Run explicitly: python -m cuhk_shenzhen_web2api.v2."""

import asyncio
import os
from contextlib import contextmanager

import uvicorn

from .app import create_app
from .config import Settings
from .transport import TransportError, build_transport


@contextmanager
def instance_lock(directory):
    """Prevent a second scheduler from recovering a running instance's work."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "instance.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError(
                    "Another v2 process owns this data directory"
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError(
                    "Another v2 process owns this data directory"
                ) from None
        yield


async def run(settings):
    transport = await build_transport(settings.backend, settings.data_dir, settings)
    try:
        await transport.authenticate()
    except TransportError as exc:
        print(
            f"Authentication not ready: {exc.code}; use the manual diagnosis workflow."
        )
    app = create_app(settings, transport)
    await uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=settings.port, workers=1)
    ).serve()


def main():
    settings = Settings.load()
    with instance_lock(settings.data_dir):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
