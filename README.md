# Vadio — Video Translator

A local web app that detects a video's spoken language, translates it into a selected language, generates narration, and exports a dubbed MP4, WAV audio, and SRT subtitles.

## Start on Windows

Requires Python 3.10 or newer and a Google AI Studio API key with access and available quota for the configured Gemini models.

1. Copy `.env.example` to `.env` and set `GEMINI_API_KEY` in that file.
2. From this folder in PowerShell, run `./start.ps1`.
3. Open http://127.0.0.1:8000.

If PowerShell blocks scripts, use these commands instead:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On macOS/Linux, use `.venv/bin/python` instead. FFmpeg is bundled by `imageio-ffmpeg`; no separate FFmpeg installation is needed. Set `IMAGEIO_FFMPEG_EXE` to use an existing installation if needed.

The interface works without an API key, but translation is disabled until the key is configured. Never put your key into frontend files or commit `.env`.

## Use

Upload an MP4, MOV, WebM, MKV, AVI, or M4V (up to 200 MB and 10 minutes). Select one of 18 target languages and a narration voice, then select **Translate video**. The app automatically detects the source language and displays progress. When complete, preview or download your translated video, audio, and subtitles. The last job is restored when you refresh the browser.

## How it works

FastAPI serves a plain HTML/CSS/JavaScript interface. A two-worker background executor processes jobs:

1. Validate the media and extract mono audio with FFmpeg.
2. Detect the language, transcribe, and translate speech in one structured Gemini request (`gemini-3.5-flash`). Timestamps are model estimates.
3. Generate each translated segment with `gemini-3.8-flash-tts`, adjust overflow pacing, and place it at its original timestamp.
4. Render an H.264/AAC MP4 and save translated audio and subtitles.

Set `GEMINI_MODEL` and `GEMINI_TTS_MODEL` in `.env` to change models. Google model availability and quota depend on your project. The key is sent only to Google's Gemini API. See the official [audio understanding](https://ai.google.dev/gemini-api/docs/generate-content/audio) and [speech generation](https://ai.google.dev/gemini-api/docs/generate-content/speech-generation) documentation.

## Scope and storage

Temporary connection failures and HTTP 408/429/500/502/503/504 responses retry up to three times with exponential backoff. Progress shows when a retry is underway. After persistent service failures, transcription can fall back to `gemini-3.1-flash-lite` and 3.8 speech to `gemini-3.8-flash-lite-tts`. Set `GEMINI_FALLBACK_MODEL` and `GEMINI_TTS_FALLBACK_MODEL` to customize these, or to an empty value to disable fallback. Authentication failures and exhausted quota do not trigger a model switch. Persistent outages can still fail a job.

- This is a single-user local application. Keep the default localhost binding. Public deployment needs authentication, upload limits at a reverse proxy, a durable queue, and per-user access control.
- Narration is synthetic; this version does not clone speakers or lip-sync. All speakers use the selected voice. Translated speech replaces the entire original audio, including background music. Long translations are sped up to fit their speech window.
- Language detection, translation, pronunciation, and timing can be imperfect, particularly with music, multiple speakers, or mixed languages.
- Audio and transcript text are sent to Google AI Studio. API usage is subject to your Google project quota and billing plan. Videos render locally. The interface loads optional fonts from Google Fonts, with system-font fallback.
- Job metadata, transcripts, and outputs remain under `data/<job-id>/`. Source videos and intermediate audio are removed after processing. Remove finished job folders to reclaim disk space. There is no automatic output expiration.
- Running jobs cannot resume across a server restart; the app marks them failed with a retry message. Use a single Uvicorn worker.

## Verify

```powershell
.venv/Scripts/python.exe -m pytest -q
```

Tests use mocked AI calls and real FFmpeg media processing. Live translation requires a valid API key and is not exercised by the automated suite.
