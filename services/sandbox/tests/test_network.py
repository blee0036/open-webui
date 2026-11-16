"""Outbound denial tests do not require Docker or real public DNS."""

import asyncio
import importlib.util
import socket
from pathlib import Path

import pytest


def proxy(monkeypatch):
    monkeypatch.setenv('SANDBOX_EGRESS_DENY_HOSTS', 'platform.example.com')
    monkeypatch.setenv('SANDBOX_EGRESS_DENY_CIDRS', '8.8.8.0/24')
    spec = importlib.util.spec_from_file_location('team_test_proxy', Path(__file__).parents[1] / 'public_proxy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_platform_alias_and_direct_ip_are_denied(monkeypatch):
    module = proxy(monkeypatch)

    async def resolve(host, port, **kwargs):
        ip = (
            '93.184.216.34'
            if host in {'platform.example.com', 'alias.example.com', '93.184.216.34'}
            else '93.184.215.14'
        )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    for host in ('platform.example.com', 'alias.example.com', '93.184.216.34'):
        with pytest.raises(ValueError):
            await module.public_address(host, 443)
    assert await module.public_address('public.example.com', 443) == '93.184.215.14'


def test_tunnel_metadata_and_control_ranges_are_denied(monkeypatch):
    module = proxy(monkeypatch)
    for address in (
        '127.0.0.1',
        '10.0.0.1',
        '169.254.169.254',
        '224.0.0.1',
        '8.8.8.8',
        '::1',
        '::ffff:8.8.8.8',
        '64:ff9b::808:808',
        '2002:0808:0808::1',
    ):
        assert not module.public_ip(address), address
    assert module.public_ip('1.1.1.1')
