"""Public HTTP(S) only; resolve once, validate every address, connect to a pinned IP."""

import asyncio
import ipaddress
import os
import socket
import time
from urllib.parse import urlsplit

DENIED_HOSTS = {
    value.strip().lower().rstrip('.')
    for value in os.getenv('SANDBOX_EGRESS_DENY_HOSTS', '').split(',')
    if value.strip()
}
DENIED_NETWORKS = [
    ipaddress.ip_network(value.strip())
    for value in os.getenv('SANDBOX_EGRESS_DENY_CIDRS', '').split(',')
    if value.strip()
]
_platform_addresses = set()
_platform_refreshed = float('-inf')
_platform_lock = asyncio.Lock()


async def platform_addresses():
    """Also deny platform IPs reached through aliases or direct-IP URLs.

    Retain previously observed addresses across DNS changes or lookup failures;
    administrators can additionally deny whole control-plane ranges with CIDRs.
    """
    global _platform_refreshed
    async with _platform_lock:
        if time.monotonic() - _platform_refreshed > 60:
            for hostname in DENIED_HOSTS:
                try:
                    answers = await asyncio.wait_for(
                        asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM), 5
                    )
                    _platform_addresses.update(ipaddress.ip_address(answer[4][0]) for answer in answers)
                except (OSError, TimeoutError):
                    pass
            _platform_refreshed = time.monotonic()
        return _platform_addresses


def public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or any(address in net for net in DENIED_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
    return True


async def public_address(host, port):
    if port not in {80, 443} or not host:
        raise ValueError('Only public HTTP(S) ports are allowed')
    normalized = host.lower().rstrip('.')
    if any(normalized == name or normalized.endswith('.' + name) for name in DENIED_HOSTS):
        raise ValueError('Platform control-plane addresses are forbidden')
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    platform = await platform_addresses()
    if not addresses or any(
        not public_ip(row[4][0]) or ipaddress.ip_address(row[4][0]) in platform for row in addresses
    ):
        raise ValueError('Private, loopback, metadata and reserved addresses are forbidden')
    return addresses[0][4][0]


async def relay(reader, writer):
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def handle(reader, writer):
    remote = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
        if len(header) > 65536:
            raise ValueError('Header too large')
        lines = header.decode('latin-1').split('\r\n')
        method, target, version = lines[0].split(' ')
        if version not in {'HTTP/1.0', 'HTTP/1.1'}:
            raise ValueError('Unsupported protocol')
        url = urlsplit('https://' + target if method == 'CONNECT' else target)
        if url.username or url.password or url.fragment or url.scheme not in {'http', 'https'}:
            raise ValueError('Absolute HTTP(S) target required')
        port = url.port or (443 if url.scheme == 'https' else 80)
        if method == 'CONNECT' and port != 443:
            raise ValueError('CONNECT is restricted to HTTPS')
        address = await public_address(url.hostname, port)
        upstream, remote = await asyncio.wait_for(asyncio.open_connection(address, port), 15)
        if method == 'CONNECT':
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        else:
            if url.scheme != 'http' or port != 80:
                raise ValueError('Use CONNECT for HTTPS')
            path = url.path or '/'
            if url.query:
                path += '?' + url.query
            fields = []
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(':', 1)
                if name.lower() not in {'host', 'connection', 'proxy-connection', 'proxy-authorization'}:
                    fields.append(name + ':' + value)
            fields.extend(['Host: ' + url.netloc, 'Connection: close'])
            remote.write((f'{method} {path} HTTP/1.1\r\n' + '\r\n'.join(fields) + '\r\n\r\n').encode('latin-1'))
        await writer.drain()
        tasks = [asyncio.create_task(relay(reader, remote)), asyncio.create_task(relay(upstream, writer))]
        try:
            await asyncio.wait(tasks, timeout=300, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (ValueError, OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    finally:
        if remote:
            remote.close()
        writer.close()


async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', 3128, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
