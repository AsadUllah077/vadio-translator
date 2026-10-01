import json
import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.media import inspect_video, run, timestamp, tempo_filter


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'SLOTS', threading.BoundedSemaphore(2))
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key-not-real')
    with TestClient(main.app) as test_client:
        yield test_client


def test_config_never_exposes_key(client):
    response = client.get('/api/config')
    assert response.status_code == 200
    assert response.json()['configured'] is True
    assert 'test-key-not-real' not in response.text
    assert 'ur' in response.json()['languages']
    assert client.get('/').status_code == 200


def test_missing_key(client, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY')
    response = client.post('/api/jobs', data={'target': 'ur'}, files={'file': ('test.mp4', b'123')})
    assert response.status_code == 503


@pytest.mark.parametrize('filename,target,expected', [('test.txt','ur',400), ('test.mp4','xx',400)])
def test_reject_invalid_input(client, filename, target, expected):
    assert client.post('/api/jobs', data={'target': target}, files={'file': (filename, b'123')}).status_code == expected


def test_limits_and_cleanup(client, monkeypatch):
    monkeypatch.setattr(main, 'MAX_BYTES', 10)
    assert client.post('/api/jobs', data={'target':'ur'}, files={'file':('test.mp4', b'x'*11)}).status_code == 413
    assert client.post('/api/jobs', data={'target':'ur'}, files={'file':('test.mp4', b'')}).status_code == 400
    assert not list(main.DATA.iterdir())
    assert main.SLOTS.acquire(blocking=False)
    assert main.SLOTS.acquire(blocking=False)
    response = client.post('/api/jobs', data={'target':'ur'}, files={'file':('test.mp4', b'1')})
    assert response.status_code == 429
    main.SLOTS.release()
    main.SLOTS.release()


def test_not_found(client):
    assert client.get('/api/jobs/not-a-uuid').status_code == 404
    assert client.get('/api/jobs/00000000-0000-0000-0000-000000000000/files/video').status_code == 404


def test_timing():
    assert timestamp(3661.234) == '01:01:01,234'
    assert timestamp(59.9999) == '00:01:00,000'
    assert tempo_filter(8) == 'atempo=2,atempo=2,atempo=2.000000'
    grouped = main.group_segments([
        SimpleNamespace(start=0, end=3, text=' Hello '),
        SimpleNamespace(start=3, end=5, text='world'),
        SimpleNamespace(start=8, end=12, text='Again'),
    ], 10)
    assert grouped == [{'start':0, 'end':5, 'text':'Hello world'}, {'start':8, 'end':10, 'text':'Again'}]
    with pytest.raises(ValueError, match='No speech'):
        main.group_segments([], 3)


def wait_for_job(client, job_id):
    for _ in range(200):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('complete', 'failed'):
            return job
        time.sleep(0.1)
    pytest.fail('Background processing did not finish')


def test_invalid_media_fails_cleanly(client):
    response = client.post('/api/jobs', data={'target':'ur'}, files={'file':('bad.mp4', b'not video')})
    assert response.status_code == 202
    job = wait_for_job(client, response.json()['id'])
    assert job['status'] == 'failed'
    assert 'valid video' in job['error']
    assert client.get(f'/api/jobs/{job["id"]}/files/video').status_code == 409
    assert not (main.DATA / job['id'] / 'input.mp4').exists()


def test_full_pipeline_with_mocked_ai(client, tmp_path, monkeypatch):
    sample = tmp_path / 'sample.mp4'
    run('-f', 'lavfi', '-i', 'color=c=blue:s=160x120:r=15:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', sample)
    assert 1.9 <= inspect_video(sample) <= 2.1
    ai = MagicMock()
    ai.__enter__.return_value = ai
    ai.transcribe_translate.return_value = ('english', [
        {'start': 0.2, 'end': 1.8, 'text': 'Hello world.', 'translation': '\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627\u06d4'}])
    def write_speech(text, voice, path):
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\0\0' * 48000)  # Longer than speech window: tests atempo.
    ai.speech.side_effect = write_speech
    monkeypatch.setattr(main, 'GeminiClient', lambda: ai)
    with sample.open('rb') as upload:
        response = client.post('/api/jobs', data={'target':'ur','voice':'Kore'}, files={'file':('sample.mp4',upload,'video/mp4')})
    assert response.status_code == 202
    job = wait_for_job(client, response.json()['id'])
    assert job['status'] == 'complete', json.dumps(job)
    assert job['source_language'] == 'english'
    assert job['progress'] == 100
    for kind in ('video','audio','subtitles'):
        output = client.get(f'/api/jobs/{job["id"]}/files/{kind}')
        assert output.status_code == 200
        assert len(output.content) > 20
        if kind == 'subtitles':
            assert 'سلام دنیا۔' in output.content.decode('utf-8')
            assert '00:00:00,200 --> 00:00:01,800' in output.text
    assert 1.9 <= inspect_video(main.DATA / job['id'] / 'translated.mp4') <= 2.1
    assert not (main.DATA / job['id'] / 'input.mp4').exists()
    ai.speech.assert_called_once()
