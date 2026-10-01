import json
import logging
import os
import shutil
import threading
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.gemini import GeminiClient

from app.media import fit_speech, inspect_video, run, write_subtitles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
LANGUAGES = {'en': 'English', 'ur': 'Urdu', 'hi': 'Hindi', 'ar': 'Arabic', 'es': 'Spanish',
             'fr': 'French', 'de': 'German', 'pt': 'Portuguese', 'it': 'Italian',
             'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean', 'tr': 'Turkish',
             'ru': 'Russian', 'id': 'Indonesian', 'nl': 'Dutch', 'pl': 'Polish', 'ta': 'Tamil'}
VOICES = ['Kore', 'Puck', 'Charon', 'Fenrir', 'Aoede', 'Leda']
MAX_BYTES = 200 * 1024 * 1024
EXECUTOR = ThreadPoolExecutor(max_workers=2)
SLOTS = threading.BoundedSemaphore(2)
LOCK = threading.RLock()
app = FastAPI(title='Vadio — video translator')
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


def job_dir(job_id: str) -> Path:
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, 'Translation not found.')
    path = DATA / job_id
    if not (path / 'job.json').exists():
        raise HTTPException(404, 'Translation not found.')
    return path


def save_job(folder: Path, **changes) -> dict:
    with LOCK:
        path = folder / 'job.json'
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        state.update(changes)
        temporary = folder / 'job.tmp'
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
        return state


# Interrupted jobs remain visible, with an actionable status after a restart.
for existing in DATA.glob('*/job.json'):
    try:
        state = json.loads(existing.read_text(encoding='utf-8'))
        if state.get('status') not in ('complete', 'failed'):
            save_job(existing.parent, status='failed', error='The server restarted. Please upload the video again.')
    except (OSError, ValueError):
        logging.warning('Could not recover a saved translation.')


def group_segments(raw, duration: float) -> list[dict]:
    groups = []
    for segment in raw:
        text = segment.text.strip()
        start, end = max(0, float(segment.start)), min(duration, float(segment.end))
        if not text or end <= start:
            continue
        if groups and end - groups[-1]['start'] <= 14 and start - groups[-1]['end'] < 2:
            groups[-1]['end'] = end
            groups[-1]['text'] += ' ' + text
        else:
            start = max(start, groups[-1]['end'] if groups else 0)
            if end > start:
                groups.append({'start': start, 'end': end, 'text': text})
    if not groups:
        raise ValueError('No speech was detected. Try a video with clearer spoken audio.')
    return groups


def process(folder: Path, source: Path, target: str, voice: str):
    try:
        save_job(folder, status='processing', stage='Reading video', progress=8)
        duration = inspect_video(source)
        audio = folder / 'source.mp3'
        run('-protocol_whitelist', 'file,pipe', '-i', source, '-vn', '-map', '0:a:0',
            '-ar', '16000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '48k', audio)
        save_job(folder, stage='Detecting language & transcribing', progress=18, duration=duration)
        with GeminiClient() as client:
            client.on_retry = lambda message: save_job(folder, stage=message)
            save_job(folder, stage='Detecting language & translating speech', progress=25)
            source_language, segments = client.transcribe_translate(audio, LANGUAGES[target], duration)
            save_job(folder, source_language=source_language, stage='Generating translated voice',
                     progress=58, segments=segments)
            timeline = bytearray(round(duration * 24000) * 2)
            for i, segment in enumerate(segments):
                speech, fitted = folder / 'speech.wav', folder / 'fitted.wav'
                client.speech(segment['translation'], voice, speech)
                fit_speech(speech, fitted, segment['end'] - segment['start'])
                with wave.open(str(fitted), 'rb') as wav:
                    frames = wav.readframes(wav.getnframes())
                start = round(segment['start'] * 24000) * 2
                end = min(len(timeline), round(segment['end'] * 24000) * 2, start + len(frames))
                timeline[start:end] = frames[:end-start]
                save_job(folder, progress=58 + round(30 * (i + 1) / len(segments)))
        dubbed = folder / 'translated.wav'
        with wave.open(str(dubbed), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(timeline)
        write_subtitles(segments, folder / 'subtitles.srt')
        save_job(folder, stage='Rendering your video', progress=92)
        run('-protocol_whitelist', 'file,pipe', '-i', source, '-i', dubbed,
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'fast',
            '-crf', '22', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-t', str(duration), '-movflags', '+faststart',
            folder / 'translated.mp4', timeout=900)
        save_job(folder, status='complete', stage='Your translation is ready', progress=100)
    except ValueError as error:
        save_job(folder, status='failed', error=str(error))
    except Exception:
        logging.exception('Translation failed for %s', folder.name)
        save_job(folder, status='failed', error='Processing failed. Please retry with a shorter MP4 video.')
    finally:
        for name in ('source.mp3', 'speech.wav', 'fitted.wav'):
            (folder / name).unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        SLOTS.release()


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/config')
def config():
    return {'configured': bool(os.getenv('GEMINI_API_KEY', '').strip()), 'languages': LANGUAGES,
            'provider': 'Google Gemini', 'voices': VOICES, 'max_mb': 200, 'max_minutes': 10}


@app.post('/api/jobs', status_code=202)
async def create_job(file: UploadFile = File(...), target: str = Form(...), voice: str = Form('Kore')):
    if target not in LANGUAGES or voice not in VOICES:
        raise HTTPException(400, 'Choose a supported language and voice.')
    if not os.getenv('GEMINI_API_KEY', '').strip():
        raise HTTPException(503, 'Add GEMINI_API_KEY to .env and restart the server to enable translation.')
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v'}:
        raise HTTPException(400, 'Choose an MP4, MOV, WebM, MKV, AVI, or M4V video.')
    if not SLOTS.acquire(blocking=False):
        raise HTTPException(429, 'Both translation slots are busy. Please try again shortly.')
    folder = DATA / str(uuid.uuid4())
    try:
        folder.mkdir()
        source = folder / f'input{suffix}'
        size = 0
        with source.open('wb') as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, 'The file is too large. The limit is 200 MB.')
                output.write(chunk)
        if not size:
            raise HTTPException(400, 'The video file is empty.')
        state = save_job(folder, id=folder.name, filename=Path(file.filename).name, target=target,
                         voice=voice, status='queued', stage='Waiting to start', progress=3)
        EXECUTOR.submit(process, folder, source, target, voice)
        return state
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        SLOTS.release()
        raise
    finally:
        await file.close()


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    with LOCK:
        return json.loads((job_dir(job_id) / 'job.json').read_text(encoding='utf-8'))


@app.get('/api/jobs/{job_id}/files/{kind}')
def download(job_id: str, kind: str):
    files = {'video': ('translated.mp4', 'video/mp4'), 'audio': ('translated.wav', 'audio/wav'),
             'subtitles': ('subtitles.srt', 'application/x-subrip')}
    if kind not in files:
        raise HTTPException(404, 'File not found.')
    folder = job_dir(job_id)
    if get_job(job_id)['status'] != 'complete':
        raise HTTPException(409, 'Your translation is not ready yet.')
    name, mime = files[kind]
    return FileResponse(folder / name, media_type=mime, filename=name,
                        content_disposition_type='inline' if kind == 'video' else 'attachment')
