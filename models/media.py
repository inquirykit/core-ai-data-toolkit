"""
Audio decoding for in-process speech models.

soundfile (libsndfile) reads WAV, FLAC, OGG and MP3 without ffmpeg. Without
it, only PCM WAV can be read, through the standard library.
"""

import io
import wave
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    import numpy


def decode_audio(data: bytes) -> Tuple["numpy.ndarray", int]:
    """(mono float32 samples in [-1, 1], sample rate). ValueError if it cannot be read."""
    import numpy as np

    try:
        import soundfile
    except ImportError:
        soundfile = None
    if soundfile is not None:
        try:
            samples, rate = soundfile.read(io.BytesIO(data), dtype="float32", always_2d=True)
        except Exception as e:
            raise ValueError(f"Audio could not be decoded: {e}") from None
        return samples.mean(axis=1), rate
    try:
        with wave.open(io.BytesIO(data)) as w:
            width, channels, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise ValueError("Audio could not be decoded: only PCM WAV can be read without soundfile installed") from None
    if width not in (1, 2, 4):
        raise ValueError(f"Audio could not be decoded: {8 * width}-bit WAV is not supported")
    if width == 1:  # 8-bit WAV is unsigned
        samples = (np.frombuffer(frames, np.uint8).astype(np.float32) - 128) / 128
    else:
        dtype = np.int16 if width == 2 else np.int32
        samples = np.frombuffer(frames, dtype).astype(np.float32) / float(np.iinfo(dtype).max)
    return samples.reshape(-1, channels).mean(axis=1), rate


def resample(samples: "numpy.ndarray", rate: int, target: int) -> "numpy.ndarray":
    """Linear resampling, good enough for speech recognition."""
    import numpy as np

    if rate == target or len(samples) == 0:
        return samples
    n = int(round(len(samples) * target / rate))
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)
