import re
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg


def ffmpeg(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-y', *map(str, args)],
        capture_output=True, timeout=timeout,
    )


def run(*args: str, timeout: int = 180) -> None:
    result = ffmpeg(*args, timeout=timeout)
    if result.returncode:
        raise ValueError('The video could not be processed. Try exporting it as an MP4 with audio.')


def inspect_video(path: Path) -> float:
    result = ffmpeg('-protocol_whitelist', 'file,pipe', '-i', path, timeout=30)
    info = result.stderr.decode('utf-8', errors='replace')
    duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', info)
    if not duration or 'Video:' not in info:
        raise ValueError('Please upload a valid video file.')
    if 'Audio:' not in info:
        raise ValueError('This video has no audio track. Choose a video with speech.')
    hours, minutes, seconds = map(float, duration.groups())
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0 or total > 600:
        raise ValueError('Videos must be between 1 second and 10 minutes long.')
    return total


def tempo_filter(ratio: float) -> str:
    parts = []
    while ratio > 2:
        parts.append('atempo=2')
        ratio /= 2
    while ratio < 0.5:
        parts.append('atempo=0.5')
        ratio *= 2
    parts.append(f'atempo={ratio:.6f}')
    return ','.join(parts)


def fit_speech(source: Path, output: Path, seconds: float) -> None:
    with wave.open(str(source), 'rb') as audio:
        # Streamed WAV headers may report an unknown (placeholder) frame count.
        frames = audio.readframes(audio.getnframes())
        duration = len(frames) / (audio.getsampwidth() * audio.getnchannels() * audio.getframerate())
    if duration <= 0 or seconds <= 0:
        raise ValueError('The speech service returned empty audio. Please try again.')
    # Keep normal pacing when the translation fits. Speed up only on overflow.
    filters = tempo_filter(max(1, duration / seconds))
    run('-i', source, '-af', f'{filters},apad,atrim=duration={seconds:.6f}',
        '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le', output)


def timestamp(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'


def write_subtitles(segments: list[dict], path: Path) -> None:
    path.write_text('\n\n'.join(
        f'{i}\n{timestamp(s["start"])} --> {timestamp(s["end"])}\n{s["translation"]}'
        for i, s in enumerate(segments, 1)
    ) + '\n', encoding='utf-8')
