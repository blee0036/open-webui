"""DNS-pinned outbound clients for member-owned MCP connections and their OAuth flow."""

import asyncio
import os
import socket
from contextvars import ContextVar
from urllib.parse import urlsplit

import aiohttp
from open_webui.utils.apps import _is_public_ip_address, get_app_url_origin

personal_oauth = ContextVar('personal_mcp_oauth', default=False)


def allowed_private():
    return {
        get_app_url_origin(value.strip())
        for value in os.getenv('PERSONAL_MCP_ALLOWED_PRIVATE_ORIGINS', '').split(',')
        if value.strip()
    }


def validate_endpoint(url):
    origin = get_app_url_origin(url)
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or parsed.query:
        raise ValueError('Use an HTTP(S) MCP URL without query credentials')
    if parsed.scheme != 'https' and origin not in allowed_private():
        raise ValueError('Use HTTPS; private HTTP origins require administrator authorization')
    return url


def private_host_allowed(host, port):
    for origin in allowed_private():
        parsed = urlsplit(origin)
        allowed_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if parsed.hostname == host and allowed_port == port:
            return True
    return False


async def resolved_address(host, port):
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or (
        not private_host_allowed(host, port) and any(not _is_public_ip_address(address[4][0]) for address in addresses)
    ):
        raise ValueError('MCP endpoint resolves to a prohibited address')
    return addresses[0][4][0]


class MemberConnector(aiohttp.TCPConnector):
    async def _resolve_host(self, host, port, traces=None):
        # Use a numeric, checked address for this exact connection, not a second DNS lookup.
        address = await resolved_address(host, port)
        return await super()._resolve_host(address, port, traces)


def oauth_http_session(*, restricted=None):
    restricted = personal_oauth.get() if restricted is None else restricted
    if restricted:
        return aiohttp.ClientSession(connector=MemberConnector(), trust_env=False)
    return aiohttp.ClientSession(trust_env=True)


def member_http_transport(*, version=2, verify=True):
    # Both clients expose AsyncConnectionPool.network_backend; keep HTTP/SNI handling
    # in their mature transports and replace only TCP resolution. Covered by tests.
    if version == 2:
        import httpcore2 as core
        import httpx2 as http
    else:
        import httpcore as core
        import httpx as http

    class PublicBackend(core.AnyIOBackend):
        async def connect_tcp(self, host, port, **kwargs):
            address = await resolved_address(host, port)
            return await super().connect_tcp(address, port, **kwargs)

    class MemberTransport(http.AsyncHTTPTransport):
        def __init__(self):
            super().__init__(verify=verify, trust_env=False)
            self._pool._network_backend = PublicBackend()

        async def handle_async_request(self, request):
            validate_endpoint(str(request.url.copy_with(query=None)))
            return await super().handle_async_request(request)

    return MemberTransport()
