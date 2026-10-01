import base64
import io
import json
import wave

import httpx
import pytest

from app.gemini import GeminiClient, GeminiError


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-google-key')
    monkeypatch.setattr('app.gemini.time.sleep', lambda seconds: None)
    with GeminiClient() as client:
        yield client


@pytest.mark.parametrize('status,match', [(429, 'quota'), (403, 'access'), (404, 'unavailable'), (503, 'temporarily')])
def test_safe_provider_errors(provider, status, match):
    provider.http.close()
    provider.http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': {'message': 'sensitive details'}})),
        base_url='https://generativelanguage.googleapis.com/v1beta/')
    with pytest.raises(GeminiError, match=match) as error:
        provider.generate('test-model', [], {})
    assert 'sensitive details' not in str(error.value)


@pytest.mark.parametrize('wav_container', [True, False])
def test_speech_handles_wav_and_pcm(provider, tmp_path, monkeypatch, wav_container):
    raw = b'\x01\x00' * 2400
    if wav_container:
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(raw)
        raw = buffer.getvalue()
    monkeypatch.setattr(provider, 'generate', lambda *args: [{'inlineData': {
        'mimeType': 'audio/wav' if wav_container else 'audio/L16;rate=24000',
        'data': base64.b64encode(raw).decode(),
    }}])
    path = tmp_path / 'speech.wav'
    provider.speech('hello', 'Kore', path)
    with wave.open(str(path), 'rb') as result:
        assert result.getnframes() == 2400
        assert result.getframerate() == 24000


def test_transcript_validates_and_clamps_timestamps(provider, tmp_path, monkeypatch):
    audio = tmp_path / 'test.mp3'
    audio.write_bytes(b'audio')
    result = {'language': 'English', 'segments': [
        {'start': -1, 'end': 2, 'text': 'Hello', 'translation': 'Bonjour'},
        {'start': 1, 'end': 9, 'text': 'World', 'translation': 'Monde'},
    ]}
    monkeypatch.setattr(provider, 'generate', lambda *args: [{'text': json.dumps(result)}])
    language, segments = provider.transcribe_translate(audio, 'French', 5)
    assert language == 'English'
    assert [(s['start'], s['end']) for s in segments] == [(0, 2), (2, 5)]
    result['segments'][0]['start'] = float('nan')
    with pytest.raises(GeminiError, match='invalid transcript'):
        provider.transcribe_translate(audio, 'French', 5)


def test_temporary_failure_retries_then_succeeds(provider):
    requests, updates = [], []
    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, json={'error': {}})
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]})
    provider.http.close()
    provider.http = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://example.test/')
    provider.on_retry = updates.append
    assert provider.generate('primary', [], {}) == [{'text': 'OK'}]
    assert len(requests) == 3
    assert len(updates) == 2


@pytest.mark.parametrize('status,expected_calls', [(503, 5), (403, 1), (429, 4)])
def test_fallback_only_on_transient_service_errors(provider, status, expected_calls):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if 'backup' in request.url.path:
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]})
        return httpx.Response(status, json={'error': {}})
    provider.http.close()
    provider.http = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://example.test/')
    if status == 503:
        assert provider.generate('primary', [], {}, 'backup') == [{'text': 'OK'}]
    else:
        with pytest.raises(GeminiError):
            provider.generate('primary', [], {}, 'backup')
    assert len(requests) == expected_calls
