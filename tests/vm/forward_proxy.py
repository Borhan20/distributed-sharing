"""A tiny forward proxy for the local VM test (tests/vm/README.md).

Some development machines block traffic forwarded from the LXD bridge (Docker and ufw both set
FORWARD to drop). Instead of changing the host firewall, the test VM reaches the internet through
this proxy, which the installer supports anyway through the usual http_proxy/https_proxy settings.

    python tests/vm/forward_proxy.py --listen 10.188.221.1:3128

Listen only on the bridge address, so nothing outside this machine's VMs can use it.
Handles CONNECT (HTTPS) and plain HTTP requests with an absolute URI (apt).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

log = logging.getLogger("forward_proxy")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        client_w.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    try:
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 20)
            client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await client_w.drain()
        else:
            u = urlsplit(target)
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 80), 20)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            hop = ("proxy-connection:", "proxy-authorization:")
            headers = [h for h in lines[1:] if h and not h.lower().startswith(hop)]
            up_w.write(f"{method} {path} {version}\r\n".encode("latin-1") + "\r\n".join(headers).encode("latin-1")
                       + b"\r\n\r\n")
            await up_w.drain()
        log.info("%s %s", method, target)
    except (OSError, TimeoutError, ValueError) as exc:
        client_w.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{exc}\r\n".encode())
        with contextlib.suppress(Exception):
            await client_w.drain()
            client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main(listen: str) -> None:
    host, _, port = listen.rpartition(":")
    server = await asyncio.start_server(handle, host, int(port))
    log.warning("forward proxy listening on %s", listen)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True, help="bridge address:port, e.g. 10.188.221.1:3128")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main(ap.parse_args().listen))
