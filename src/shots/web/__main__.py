"""python -m shots.web [--port 8000]: serve the board on 127.0.0.1 only.

Settings (environment):
  SHOTS_PROXY_HEADERS=1         trust X-Forwarded-For / X-Forwarded-Proto (only behind a TLS reverse proxy;
                                default off: the client IP and scheme are the socket's)
  SHOTS_FORWARDED_ALLOW_IPS     proxies allowed to set those headers (default 127.0.0.1)
  SHOTS_ACCESS_LOG=1            enable uvicorn's access log (default off: it would log client IPs and URLs)
  SHOTS_ALLOWED_HOSTS           Host header allow-list, comma-separated (default 127.0.0.1,localhost)
"""

import argparse
import os

import uvicorn

_TRUE = ("1", "true", "yes", "on")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else raw.strip().lower() in _TRUE


def server_options(host: str = "127.0.0.1", port: int = 8000) -> dict:
    """uvicorn.run keyword arguments; proxy and logging settings are always explicit (never uvicorn's
    defaults, which trust X-Forwarded-* from 127.0.0.1 and log every request with its client IP)."""
    return dict(host=host, port=port, log_level="info",
                proxy_headers=_flag("SHOTS_PROXY_HEADERS"),
                forwarded_allow_ips=os.environ.get("SHOTS_FORWARDED_ALLOW_IPS", "").strip() or "127.0.0.1",
                access_log=_flag("SHOTS_ACCESS_LOG"),
                server_header=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="local access only by default")
    args = ap.parse_args(argv)
    uvicorn.run("shots.web.app:app", **server_options(args.host, args.port))


if __name__ == "__main__":
    main()
