"""Bounded PCM segments for explicitly metered Realtime input transcription.

Server VAD remains enabled, but automatic upstream ASR is disabled while quotas
are enforced. Each committed speech segment becomes one gated HTTP inference,
so a provider cannot start billable ASR before the platform reserves its quota.
"""

import base64
import io
import wave
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from fastapi import HTTPException
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.utils.quota_transport import metered_request
from open_webui.utils.session_pool import get_session


class RealtimeAudioSegments:
    BYTES_PER_MS = 48  # 24 kHz, mono, 16-bit PCM
    MAX_BYTES = 122_000 * BYTES_PER_MS

    def __init__(self):
        self.audio = bytearray()
        self.offset = 0
        self.written = 0
        self.committed = 0
        self.starts = {}
        self.ready = {}
        self.manual_ends = []

    def command(self, event):
        kind = event.get('type')
        if kind == 'input_audio_buffer.append':
            pcm = base64.b64decode(event['audio'], validate=True)
            self.audio.extend(pcm)
            self.written += len(pcm)
            excess = len(self.audio) - self.MAX_BYTES
            if excess > 0:
                # Keep enough silence/prefix padding for the next VAD start.
                trim = (excess + self.BYTES_PER_MS - 1) // self.BYTES_PER_MS * self.BYTES_PER_MS
                del self.audio[:trim]
                self.offset += trim
                if any(start < self.offset for start in self.starts.values()):
                    raise ValueError('Voice segment exceeds two minutes. Pause before continuing.')
        elif kind == 'input_audio_buffer.commit':
            start = max(self.offset, self.committed, self.manual_ends[-1][0] if self.manual_ends else 0)
            if self.starts:
                start = max(start, min(self.starts.values()))
            if not start < self.written or self.written - start > 120_000 * self.BYTES_PER_MS:
                raise ValueError('Voice segment is empty or exceeds two minutes')
            if len(self.manual_ends) >= 16:
                raise ValueError('Too many voice segments are waiting for transcription')
            # Snapshot now: a subsequent clear must not erase an already committed
            # request whose acknowledgement is still in flight from the provider.
            self.manual_ends.append((self.written, bytes(self.audio[start - self.offset :])))
        elif kind == 'input_audio_buffer.clear':
            self.audio.clear()
            self.offset = self.written
            self.committed = self.written
            self.starts.clear()

    def observe(self, event):
        kind, item = event.get('type'), event.get('item_id')
        if kind == 'input_audio_buffer.speech_started':
            start = event.get('audio_start_ms')
            if type(start) is not int or not self.offset <= start * self.BYTES_PER_MS <= self.written:
                raise ValueError('Voice provider returned an unavailable audio segment')
            self.starts[item] = start * self.BYTES_PER_MS
        elif kind == 'input_audio_buffer.speech_stopped':
            start = self.starts.pop(item, None)
            end = event.get('audio_end_ms')
            if type(end) is not int or start is None:
                raise ValueError('Voice provider returned an invalid audio segment')
            end *= self.BYTES_PER_MS
            if not self.offset <= start < end <= self.written or end - start > 120_000 * self.BYTES_PER_MS:
                raise ValueError('Voice segment is unavailable or exceeds two minutes')
            if len(self.ready) >= 16:
                raise ValueError('Too many voice segments are waiting for transcription')
            self.ready[item] = (end, bytes(self.audio[start - self.offset : end - self.offset]))
        elif kind == 'input_audio_buffer.committed':
            segment = self.ready.pop(item, None)
            if segment is None and self.manual_ends:
                segment = self.manual_ends.pop(0)
            self.starts.pop(item, None)
            if not segment:
                raise ValueError('Voice provider committed an unavailable audio segment')
            end, pcm = segment
            self.committed = max(self.committed, end)
            return item, pcm


async def transcribe_segment(request, user, config, headers, pcm):
    """Use the configured call provider's ASR model, without a hidden ASR stream."""
    target = urlsplit(config['audio.realtime.openai.api_base_url'])
    url = urlunsplit((target.scheme, target.netloc, target.path.rstrip('/') + '/audio/transcriptions', '', ''))
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)
    form = aiohttp.FormData()
    model = config['audio.realtime.transcription_model']
    form.add_field('model', model)
    form.add_field('response_format', 'json')
    form.add_field('file', audio.getvalue(), filename='speech.wav', content_type='audio/wav')
    # A conservative audio token reservation; returned usage is authoritative.
    estimate = (len(pcm) + 479) // 480
    async with metered_request(
        await get_session(),
        request,
        user,
        model_id=model,
        url=url,
        headers=headers,
        data=form,
        ssl=AIOHTTP_CLIENT_SESSION_SSL,
        estimated_usage={
            'input_tokens': estimate,
            'output_tokens': 1024,
            'input_token_details': {'audio_tokens': estimate},
        },
    ) as response:
        if response.status >= 400:
            raise HTTPException(502, 'Voice transcription was rejected by the configured provider.')
        result = await response.json()
    if not isinstance(result.get('text'), str):
        raise ValueError('Voice provider returned an invalid transcript')
    return result['text']
