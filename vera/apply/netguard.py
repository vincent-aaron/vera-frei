"""The form engine's browser only talks to the public internet.

Two layers:
1. A small filtering proxy (FilterProxy) that every connection of the engine's browser goes through: pages,
   redirects, pop-ups, frames, form posts, scripts, websockets, service workers. For each connection it looks the
   name up itself, refuses anything that isn't on the public internet, and connects to exactly the address it
   checked, so a name that changes its answer (DNS rebinding) can't slip through.
2. guard() on the browser context, which also blocks file:// and other non-web schemes before they're requested.

Anything aimed at this laptop (localhost), the home network (192.168.x.x, the router) or another private address
is refused. A job link from a stranger can then only ever reach public websites.

Standalone on purpose: the engine never imports vera.config, which would read the .env secrets back in.
"""

import asyncio
import ipaddress
import socket
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ALWAYS_OK = ("about", "data", "blob")
ALLOWED_PORTS = {80, 443, 8080, 8443}
NAT64 = ipaddress.ip_network("64:ff9b::/96")
IPV4_COMPAT = ipaddress.ip_network("::/96")
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".home.arpa")
_public: Dict[str, bool] = {}


def ip_is_public(ip) -> bool:
    """A real public internet address. IPv6 forms that wrap an IPv4 address are judged by that address."""
    try:
        ip = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip in IPV4_COMPAT or ip in NAT64 or ip.sixtofour is not None or ip.teredo is not None:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified


def local_name(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    return not host or host == "localhost" or host.endswith(LOCAL_SUFFIXES)


def resolve_public(host: str, port: int = 443) -> Optional[List[str]]:
    """The host's addresses if every one of them is public, else None. Not cached: each connection checks again."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if local_name(host):
        return None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return None
    addrs = list(dict.fromkeys(info[4][0] for info in infos))
    return addrs if addrs and all(ip_is_public(a) for a in addrs) else None


def host_is_public(host: str) -> bool:
    """Every address the name resolves to is on the public internet (cached for the quick guard() check)."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if local_name(host):
        return False
    if host in _public:
        return _public[host]
    ok = resolve_public(host) is not None
    if ok:
        _public[host] = True             # failures aren't cached: a passing DNS hiccup shouldn't block a site all run
    return ok


def url_is_safe(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme in ALWAYS_OK:
        return True
    return parsed.scheme in ("http", "https") and host_is_public(parsed.hostname or "")


async def _guard(route) -> None:
    url = route.request.url
    scheme, host = urlparse(url).scheme, (urlparse(url).hostname or "").lower()
    if scheme in ALWAYS_OK or (scheme in ("http", "https") and (_public.get(host) or await asyncio.to_thread(url_is_safe, url))):
        await route.continue_()
    else:
        print(f"  blocked a request to {host or url[:40]} (not a public website)")
        await route.abort("blockedbyclient")


async def install(context) -> None:
    """Put the guard on a browser context: every tab and pop-up opened in it is covered."""
    await context.route("**/*", _guard)


# ----------------------------------------------------------------- the filtering proxy
class FilterProxy:
    """A tiny local HTTP proxy (CONNECT tunnels for https and websockets, plain forwarding for http) that only ever
    connects to public internet addresses it resolved and checked itself. Listens on 127.0.0.1 only."""

    def __init__(self, resolve: Callable[[str, int], Optional[List[str]]] = resolve_public):
        self.resolve = resolve
        self.server: Optional[asyncio.base_events.Server] = None
        self.port = 0
        self.blocked: List[str] = []

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _open(self, host: str, port: int) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        if port not in ALLOWED_PORTS:
            return None
        addrs = await asyncio.to_thread(self.resolve, host, port)
        if not addrs:
            return None
        for addr in addrs[:3]:                       # connect to the exact address that was checked
            try:
                return await asyncio.wait_for(asyncio.open_connection(addr, port), 20)
            except (OSError, asyncio.TimeoutError):
                continue
        return None

    async def _refuse(self, writer: asyncio.StreamWriter, target: str) -> None:
        self.blocked.append(target)
        print(f"  blocked a connection to {target[:80]} (not a public website)")
        try:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, OSError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return await self._refuse(writer, lines[0][:80])
        method, target, version = parts
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            if not port.isdigit():
                return await self._refuse(writer, target)
            upstream = await self._open(host.strip("[]"), int(port))
            if upstream is None:
                return await self._refuse(writer, target)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            return await self._pipe(reader, writer, *upstream)
        parsed = urlparse(target)                    # plain http: "GET http://host/path HTTP/1.1"
        if parsed.scheme != "http" or not parsed.hostname:
            return await self._refuse(writer, target)
        upstream = await self._open(parsed.hostname, parsed.port or 80)
        if upstream is None:
            return await self._refuse(writer, target)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-connection:", "connection:", "keep-alive:"))]
        request = f"{method} {path} {version}\r\n" + "\r\n".join(headers + ["Connection: close"]) + "\r\n\r\n"
        upstream[1].write(request.encode("latin-1"))
        # One request per connection ("Connection: close"), so the browser can't reuse it for another host.
        await self._pipe(reader, writer, *upstream)

    @staticmethod
    async def _pipe(c_reader, c_writer, u_reader, u_writer) -> None:
        async def copy(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        await asyncio.gather(copy(c_reader, u_writer), copy(u_reader, c_writer))


# Chromium flags that go with the proxy: loopback isn't exempt from it, and WebRTC can't reach around it.
BROWSER_ARGS = ["--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp"]
