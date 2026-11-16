"""A real browser-to-bridge-to-provider WebSocket path, using only local fakes."""

import asyncio
import base64
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import uvicorn
from aiohttp import web
from fastapi import FastAPI
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.team import QuotaPlan
from open_webui.routers.audio import realtime
from open_webui.utils.quotas import quota_summary
from open_webui.utils.session_pool import close_session


@pytest.mark.asyncio
async def test_bridge_disables_hidden_asr_and_gates_each_asr_and_response(monkeypatch):
    async with get_async_db() as db:
        db.add(
            QuotaPlan(
                id='voice',
                name='Voice',
                unit='requests',
                allowance=2,
                reset={'kind': 'month', 'timezone': 'Asia/Singapore'},
                created_at=0,
                updated_at=0,
            )
        )
        await db.commit()
    received = []
    settings = []

    async def provider(request):
        upstream = web.WebSocketResponse()
        await upstream.prepare(request)
        await upstream.send_json({'type': 'session.created'})
        settings.append((await upstream.receive_json())['session'])
        await upstream.send_json({'type': 'session.updated'})
        async for raw in upstream:
            event = raw.json()
            received.append(event['type'])
            if event['type'] == 'input_audio_buffer.append':
                for kind, extra in (
                    ('speech_started', {'audio_start_ms': 0}),
                    ('speech_stopped', {'audio_end_ms': 1000}),
                    ('committed', {}),
                ):
                    await upstream.send_json({'type': 'input_audio_buffer.' + kind, 'item_id': 'speech', **extra})
            if event['type'] == 'response.create':
                await upstream.send_json({'type': 'response.created', 'response': {'id': 'spoken'}})
                await upstream.send_json(
                    {
                        'type': 'response.done',
                        'response': {'id': 'spoken', 'usage': {'input_tokens': 10, 'output_tokens': 5}},
                    }
                )
        return upstream

    async def transcribe(request):
        parts = await request.post()
        assert parts['model'] == 'local-asr' and parts['file'].file.read().startswith(b'RIFF')
        received.append('http.asr')
        return web.json_response({'text': 'Hello', 'usage': {'input_tokens': 5, 'output_tokens': 1}})

    fake = web.Application()
    fake.router.add_get('/realtime', provider)
    fake.router.add_post('/audio/transcriptions', transcribe)
    runner = web.AppRunner(fake)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    await Config.upsert(
        {
            'team.quota.enabled': True,
            'team.quota.default_plan_id': 'voice',
            'audio.realtime.enabled': True,
            'audio.realtime.provider': 'openai',
            'audio.realtime.auth_type': 'api_key',
            'audio.realtime.openai.api_base_url': f'http://127.0.0.1:{port}',
            'audio.realtime.openai.api_key': 'local-only',
            'audio.realtime.model': 'local-voice',
            'audio.realtime.voice': 'test-voice',
            'audio.realtime.transcription_model': 'local-asr',
        }
    )
    monkeypatch.setattr(
        realtime, 'get_verified_user_by_token', AsyncMock(return_value=SimpleNamespace(id='speaker', role='admin'))
    )
    monkeypatch.setattr(realtime.Models, 'get_model_by_id', AsyncMock(return_value=None))
    monkeypatch.setattr(realtime, 'BYPASS_MODEL_ACCESS_CONTROL', True)
    app = FastAPI()
    app.state.MODELS = {'chat': {'id': 'chat'}}
    app.include_router(realtime.router)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', access_log=False))
    serve = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serve.done():
                    serve.result()
                await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f'http://127.0.0.1:{port}/realtime') as bridge:
                await bridge.send_json({'type': 'auth', 'token': 'test', 'model_id': 'chat'})
                assert (await bridge.receive_json(timeout=10))['type'] == 'bridge.ready'
                assert settings[0]['audio']['input']['transcription'] is None
                assert settings[0]['audio']['input']['turn_detection']['create_response'] is False
                await bridge.send_json(
                    {'type': 'input_audio_buffer.append', 'audio': base64.b64encode(b'\0\0' * 24000).decode()}
                )
                async with asyncio.timeout(10):
                    while (event := await bridge.receive_json())[
                        'type'
                    ] != 'conversation.item.input_audio_transcription.completed':
                        assert event['type'] != 'bridge.error', event
                assert event['transcript'] == 'Hello'
                await bridge.send_json({'type': 'bridge.respond', 'item_id': 'speech'})
                async with asyncio.timeout(10):
                    while (event := await bridge.receive_json())['type'] != 'response.done':
                        assert event['type'] != 'bridge.error', event
                summary = await quota_summary('speaker')
                assert summary['period']['used'] == 2 and summary['period']['reserved'] == 0
                await bridge.send_json({'type': 'bridge.status', 'status': 'working'})
                assert (await bridge.receive_json(timeout=10))['type'] == 'bridge.error'
                assert received.count('http.asr') == 1 and received.count('response.create') == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve, timeout=10)
        sock.close()
        await close_session()
        await runner.cleanup()
