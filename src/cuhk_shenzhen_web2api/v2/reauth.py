"""User-run manual login in a new auth directory, then authenticated handover."""

import argparse
import asyncio

import httpx

from .config import Settings
from .diagnose import diagnose


async def run(args):
    settings = Settings.load()
    name = args.directory
    if not name or name in {".", ".."} or any(c in name for c in "/\\:"):
        raise ValueError("Choose one new directory name")
    path = settings.auth_root.resolve() / name
    login = argparse.Namespace(
        data_dir=str(path),
        backend=settings.backend,
        chat=False,
        model=None,
        stream_mode=settings.stream_mode,
        inspect_history=False,
    )
    if settings.backend == "firecrawl":
        raise ValueError("Manual local recovery supports local backends only")
    if await diagnose(login):
        return 1
    async with httpx.AsyncClient(timeout=240, trust_env=False) as client:
        response = await client.post(
            args.server + "/auth/reload",
            headers={"Authorization": "Bearer " + settings.api_key},
            json={"directory": name},
        )
        print("Authentication handover HTTP", response.status_code)
        return 0 if response.status_code == 200 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8767")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
