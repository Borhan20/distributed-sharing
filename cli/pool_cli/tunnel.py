"""Open an app's ports on this computer (docs/design/pool-run.md step 2).

For each port, `pool` listens on 127.0.0.1 here (only this computer can connect, but any user of this computer
can). Each connection opens one WebSocket to the server's tunnel for the app's port, signed in as you; the server
joins it to the computer that runs the app, which pipes it into the app's container. Only the person who started
the app can open a tunnel; the token never reaches the app or local clients.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

FREE_PORT_TRIES = 20


def tunnel_url(server: str, job_id: str, port: int) -> str:
    u = urlsplit(server)
    scheme = {"https": "wss", "http": "ws"}[u.scheme]
    return urlunsplit((scheme, u.netloc, f"/api/v1/jobs/{job_id}/tunnel", f"port={port}", ""))


def parse_ports(values: list[str]) -> dict[int, int]:
    """docker's -p: PORT, HOST_PORT:APP_PORT or 127.0.0.1:HOST_PORT:APP_PORT (TCP). {app port: port here}."""
    out: dict[int, int] = {}
    for v in values:
        if v.endswith("/udp"):
            raise ValueError(f"-p {v}: only TCP ports can be opened")
        parts = v.removesuffix("/tcp").split(":")
        if len(parts) == 1:
            here = app = parts[0]
        elif len(parts) == 2:
            here, app = parts
        elif len(parts) == 3 and parts[0] in ("127.0.0.1", "localhost"):
            here, app = parts[1], parts[2]
        else:
            raise ValueError(f"-p {v}: use PORT or HOST_PORT:APP_PORT, e.g. -p 8000 or -p 8080:8000 (ports open "
                             "only on this computer, 127.0.0.1)")
        try:
            a, h = int(app), int(here)
        except ValueError:
            raise ValueError(f"-p {v}: ports are numbers, e.g. -p 8080:8000") from None
        if not (1 <= a <= 65535 and 1 <= h <= 65535):
            raise ValueError(f"-p {v}: ports are between 1 and 65535")
        out[a] = h
    return out


class Forwarder:
    """Listens on this computer for each app port and sends every connection through the server's tunnel."""

    def __init__(self, server: str, token: Callable[[], str | None], job_id: str, ports: dict[int, int],
                 say: Callable[[str], None]):
        # token is read for each connection, so signing in again (pool login) needs no restart.
        self.server, self.token, self.job_id, self.ports, self.say = server, token, job_id, ports, say
        self._listeners: list[socket.socket] = []
        self._stop = threading.Event()
        self.opened: dict[int, int] = {}

    def start(self) -> dict[int, int]:
        """Start listening. Returns {app port: port here} (the next free number when one is taken)."""
        for app_port, here in self.ports.items():
            sock = None
            for candidate in range(here, min(here + FREE_PORT_TRIES, 65536)):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if os.name == "nt":  # there, SO_REUSEADDR would let two programs share the port
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:  # only lets it bind again soon after an earlier run
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", candidate))
                except OSError:
                    s.close()
                    continue
                s.listen(64)
                sock, here = s, candidate
                break
            if sock is None:
                self.say(f"port {here} and the next {FREE_PORT_TRIES - 1} are taken here: cannot open app port "
                         f"{app_port}")
                continue
            self._listeners.append(sock)
            self.opened[app_port] = here
            threading.Thread(target=self._accept, args=(sock, app_port), daemon=True).start()
        return self.opened

    def stop(self) -> None:
        self._stop.set()
        for s in self._listeners:
            with contextlib.suppress(OSError):
                s.close()

    def _accept(self, sock: socket.socket, app_port: int) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, app_port), daemon=True).start()

    def _serve(self, conn: socket.socket, app_port: int) -> None:
        token = self.token()
        if not token:
            self.say("not signed in: run `pool login`, then open the page again")
            conn.close()
            return
        try:
            with connect(tunnel_url(self.server, self.job_id, app_port),
                         additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=30,
                         max_size=2**20, compression=None) as ws:
                _pump(conn, ws)
        except InvalidStatus as exc:
            self.say(f"the tunnel to port {app_port} was refused ({exc.response.status_code}): is the app still "
                     "running, and are you signed in (pool login)?")
        except (OSError, TimeoutError) as exc:
            self.say(f"cannot open a tunnel to port {app_port}: {exc}")
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def _pump(conn: socket.socket, ws: ClientConnection) -> None:
    """Copy bytes both ways until either side ends."""

    def up() -> None:  # this computer -> app
        try:
            while chunk := conn.recv(32 * 1024):
                ws.send(chunk)
        except (OSError, ConnectionClosed):
            pass
        finally:
            with contextlib.suppress(Exception):
                ws.close()

    threading.Thread(target=up, daemon=True).start()
    try:
        for msg in ws:  # app -> this computer
            if isinstance(msg, bytes):
                conn.sendall(msg)
    except (OSError, ConnectionClosed):
        pass
