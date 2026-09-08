"""Task: run the local web2api HTTP server (FastAPI/uvicorn, 127.0.0.1:8765).

python src/cuhk_shenzhen_web2api/scripts/server.py [--port 8765]
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    uvicorn.run(
        "cuhk_shenzhen_web2api.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
