"""
encode.py - Export options (container, video, picture, audio) and the FFmpeg arguments they make.
The tables below say what each container and codec allows; ExportOptions.normalized() keeps every
combination valid. No Tk and no processes in here.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, fields, replace
from fractions import Fraction
from typing import AbstractSet, Any, Dict, List, Optional, Tuple

NO_AUDIO = "none"
"""Audio codec code of an export without sound."""

COPY_AUDIO = "copy"
"""Audio codec code of an export that copies the source audio as it is (no encoding). Only possible when the
project has exactly one clip with sound that is not changed (see Job.audio_source)."""

RATE_QUALITY, RATE_BITRATE = "quality", "bitrate"
"""Rate control: constant quality (CRF) or average bitrate (optionally two-pass)."""

BITRATE_RANGE = (50, 200000)
"""Allowed video bitrate in kb/s."""

HEIGHTS = (0, 2160, 1440, 1080, 720, 576, 480)
"""Output heights in pixels; 0 = the project size."""

FPS_CHOICES = ("", "24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60")
"""Output frame rates as FFmpeg writes them; \"\" = the project rate."""

CHANNELS = (0, 1, 2)
"""Audio channels: 0 = as mixed by the project, 1 = mono, 2 = stereo."""

SAMPLE_RATES = (0, 44100, 48000)
"""Audio sample rates in Hz; 0 = as mixed by the project."""

_X26X_SPEEDS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")


@dataclass(frozen=True)
class Container:
    """A file format: label and file extension."""
    label: str
    ext: str


@dataclass(frozen=True)
class VideoCodec:
    """
    What one video encoder offers.

    @param containers  codes of the containers it can go into
    @param crf         (lowest, highest, default) quality value; lower = better picture
    @param speed_flag  FFmpeg option that takes the speed value
    @param speeds      speed values, fastest first (x264/x265) or slowest first (the numbered ones)
    @param speed       default speed
    @param tunes       tune values, () = none offered
    @param profiles    profile values, () = none offered
    @param levels      level values, () = none offered
    @param ten_bit     True if a 10-bit picture is offered
    @param two_pass    True if two-pass average bitrate is offered
    """
    label: str
    encoder: str
    containers: Tuple[str, ...]
    crf: Tuple[int, int, int]
    speed_flag: str
    speeds: Tuple[str, ...]
    speed: str
    tunes: Tuple[str, ...] = ()
    profiles: Tuple[str, ...] = ()
    levels: Tuple[str, ...] = ()
    ten_bit: bool = False
    two_pass: bool = False


@dataclass(frozen=True)
class AudioCodec:
    """
    What one audio encoder offers.

    @param bitrates  offered bitrates in kb/s; () = lossless, no bitrate
    @param bitrate   default bitrate
    """
    label: str
    encoder: str
    containers: Tuple[str, ...]
    bitrates: Tuple[int, ...]
    bitrate: int


CONTAINERS: Dict[str, Container] = {
    "mp4": Container("MP4", ".mp4"),
    "mkv": Container("MKV", ".mkv"),
    "webm": Container("WebM", ".webm"),
}

VIDEO: Dict[str, VideoCodec] = {
    "x264": VideoCodec("H.264", "libx264", ("mp4", "mkv"), (0, 51, 21), "-preset", _X26X_SPEEDS, "medium",
                       tunes=("film", "animation", "grain", "stillimage", "fastdecode", "zerolatency"),
                       profiles=("baseline", "main", "high"),
                       levels=("3.0", "3.1", "4.0", "4.1", "4.2", "5.0", "5.1", "5.2"), two_pass=True),
    "x265": VideoCodec("H.265", "libx265", ("mp4", "mkv"), (0, 51, 25), "-preset", _X26X_SPEEDS, "medium",
                       tunes=("animation", "grain", "fastdecode", "zerolatency"), ten_bit=True),
    "vp9": VideoCodec("VP9", "libvpx-vp9", ("mkv", "webm"), (0, 63, 31), "-cpu-used",
                      tuple(str(i) for i in range(6)), "2", ten_bit=True, two_pass=True),
    "svtav1": VideoCodec("AV1 (SVT-AV1)", "libsvtav1", ("mp4", "mkv", "webm"), (0, 63, 32), "-preset",
                         tuple(str(i) for i in range(14)), "8", ten_bit=True),
    "aom": VideoCodec("AV1 (libaom)", "libaom-av1", ("mp4", "mkv", "webm"), (0, 63, 32), "-cpu-used",
                      tuple(str(i) for i in range(9)), "4", ten_bit=True),
}

AUDIO: Dict[str, AudioCodec] = {
    "aac": AudioCodec("AAC", "aac", ("mp4", "mkv"), (96, 128, 160, 192, 256, 320), 192),
    "mp3": AudioCodec("MP3", "libmp3lame", ("mp4", "mkv"), (128, 160, 192, 256, 320), 192),
    "ac3": AudioCodec("AC-3", "ac3", ("mp4", "mkv"), (192, 256, 384, 448, 640), 384),
    "opus": AudioCodec("Opus", "libopus", ("mkv", "webm"), (64, 96, 128, 160, 192, 256), 160),
    "vorbis": AudioCodec("Vorbis", "libvorbis", ("mkv", "webm"), (96, 128, 160, 192, 256), 160),
    "flac": AudioCodec("FLAC", "flac", ("mkv",), (), 0),
}

_BITRATE_ENCODERS_NEEDING_ZERO = ("libvpx-vp9", "libaom-av1")
"""Encoders that need -b:v 0 next to -crf to run in constant quality mode."""


def fps_value(text: str) -> float:
    """@return the frame rate of an FPS_CHOICES entry as a number, 0.0 for \"\" (project rate)"""
    return float(Fraction(text)) if text else 0.0


# --------------------------------------------------------------------------- #
# What this FFmpeg can encode
# --------------------------------------------------------------------------- #


def video_codecs(container: str, encoders: Optional[AbstractSet[str]] = None) -> List[str]:
    """@return codes of the video codecs that fit a container (and are in `encoders`, if given)"""
    return [c for c, v in VIDEO.items() if container in v.containers and (encoders is None or v.encoder in encoders)]


def audio_codecs(container: str, encoders: Optional[AbstractSet[str]] = None) -> List[str]:
    """@return codes of the audio codecs that fit a container (and are in `encoders`, if given)"""
    return [c for c, a in AUDIO.items() if container in a.containers and (encoders is None or a.encoder in encoders)]


def containers(encoders: Optional[AbstractSet[str]] = None) -> List[str]:
    """@return codes of the containers that have at least one usable video codec"""
    return [c for c in CONTAINERS if video_codecs(c, encoders)]


def _pick(value: Any, allowed, default: Any) -> Any:
    return value if value in allowed else default


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportOptions:
    """
    Everything the user can choose for an export. Use normalized() on anything that did not come from the tables above.

    @param container      key of CONTAINERS
    @param video          key of VIDEO
    @param rate           RATE_QUALITY or RATE_BITRATE
    @param crf            quality value for RATE_QUALITY
    @param bitrate        video kb/s for RATE_BITRATE
    @param two_pass       two passes for RATE_BITRATE (needs VideoCodec.two_pass)
    @param speed          speed value of the encoder
    @param tune           tune value, "" = none
    @param profile        profile value, "" = encoder decides
    @param level          level value, "" = encoder decides
    @param bit_depth      8 or 10
    @param fps            entry of FPS_CHOICES
    @param height         entry of HEIGHTS
    @param audio          key of AUDIO, NO_AUDIO or COPY_AUDIO
    @param audio_bitrate  audio kb/s (0 for lossless codecs)
    @param channels       entry of CHANNELS
    @param sample_rate    entry of SAMPLE_RATES
    @param faststart      MP4 only: move the index to the front so the file plays while it downloads
    @param extra          extra FFmpeg output options, written like a command line
    """
    container: str = "mp4"
    video: str = "x264"
    rate: str = RATE_QUALITY
    crf: int = 21
    bitrate: int = 8000
    two_pass: bool = False
    speed: str = "medium"
    tune: str = ""
    profile: str = ""
    level: str = ""
    bit_depth: int = 8
    fps: str = ""
    height: int = 0
    audio: str = "aac"
    audio_bitrate: int = 192
    channels: int = 0
    sample_rate: int = 0
    faststart: bool = True
    extra: str = ""

    def normalized(self, encoders: Optional[AbstractSet[str]] = None) -> ExportOptions:
        """
        @param encoders  names of the encoders this FFmpeg has; None = do not check
        @return          options where every value is allowed and the combination fits (wrong values replaced by defaults)
        """
        usable = containers(encoders)
        container = _pick(self.container, usable, usable[0] if usable else "mp4")
        codecs = video_codecs(container, encoders)
        video = _pick(self.video, codecs, codecs[0] if codecs else "x264")
        v = VIDEO[video]
        rate = _pick(self.rate, (RATE_QUALITY, RATE_BITRATE), RATE_QUALITY)
        sounds = audio_codecs(container, encoders)
        audio = _pick(self.audio, sounds + [NO_AUDIO, COPY_AUDIO], sounds[0] if sounds else NO_AUDIO)
        lossy = AUDIO[audio].bitrates if audio in AUDIO else ()
        return replace(
            self, container=container, video=video, rate=rate,
            crf=_clamp(self.crf, v.crf[0], v.crf[1]),
            bitrate=_clamp(self.bitrate, *BITRATE_RANGE),
            two_pass=bool(self.two_pass and v.two_pass and rate == RATE_BITRATE),
            speed=_pick(self.speed, v.speeds, v.speed),
            tune=_pick(self.tune, v.tunes, ""),
            profile=_pick(self.profile, v.profiles, ""),
            level=_pick(self.level, v.levels, ""),
            bit_depth=10 if self.bit_depth == 10 and v.ten_bit else 8,
            fps=_pick(self.fps, FPS_CHOICES, ""),
            height=_pick(self.height, HEIGHTS, 0),
            audio=audio,
            audio_bitrate=_pick(self.audio_bitrate, lossy, AUDIO[audio].bitrate if audio in AUDIO else 0),
            channels=_pick(self.channels, CHANNELS, 0),
            sample_rate=_pick(self.sample_rate, SAMPLE_RATES, 0),
            faststart=bool(self.faststart),
            extra=self.extra.strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """@return the options as a dict for the config file"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any, encoders: Optional[AbstractSet[str]] = None) -> ExportOptions:
        """
        @param data      dict from a config file; unknown keys are ignored, wrong values replaced by defaults
        @param encoders  see normalized()
        """
        base, values = cls(), {}
        for f in fields(cls):
            default = getattr(base, f.name)
            raw = data.get(f.name, default) if isinstance(data, dict) else default
            if isinstance(default, bool):
                values[f.name] = raw if isinstance(raw, bool) else default
                continue
            try:
                values[f.name] = default if raw is None else type(default)(raw)
            except (TypeError, ValueError):
                values[f.name] = default
        return cls(**values).normalized(encoders)

    def extra_args(self) -> List[str]:
        """@return the extra options as an argument list @raise ValueError if the text cannot be split (quotes)"""
        return shlex.split(self.extra)

    def summary(self) -> str:
        """@return a short description for lists, for example \"MP4, H.264, CRF 21, 720p, AAC 192 kb/s\""""
        parts = [CONTAINERS[self.container].label, VIDEO[self.video].label]
        if self.rate == RATE_QUALITY:
            parts.append("CRF %d" % self.crf)
        else:
            parts.append("%d kb/s%s" % (self.bitrate, ", 2-pass" if self.two_pass else ""))
        if self.height:
            parts.append("%dp" % self.height)
        if self.fps:
            parts.append("%g fps" % round(fps_value(self.fps), 3))
        if self.audio == COPY_AUDIO:
            parts.append("audio copy")
        elif self.audio != NO_AUDIO:
            a = AUDIO[self.audio]
            parts.append("%s %d kb/s" % (a.label, self.audio_bitrate) if a.bitrates else a.label)
        return ", ".join(parts)

    # ------------------------------------------------------------------ #
    # FFmpeg arguments
    # ------------------------------------------------------------------ #

    def video_args(self, threads: int = 0) -> List[str]:
        """
        @param threads  thread limit of the encoder, 0 = FFmpeg decides
        @return         encoder, rate control, speed, tune, profile, level, pixel format and frame rate
        """
        v = VIDEO[self.video]
        args = ["-c:v", v.encoder] + (["-threads:v", str(threads)] if threads > 0 else [])
        if self.rate == RATE_QUALITY:
            args += ["-crf", str(self.crf)]
            if v.encoder in _BITRATE_ENCODERS_NEEDING_ZERO:
                args += ["-b:v", "0"]
        else:
            args += ["-b:v", "%dk" % self.bitrate]
        args += [v.speed_flag, self.speed]
        if v.encoder in _BITRATE_ENCODERS_NEEDING_ZERO:
            args += ["-row-mt", "1"]
        for flag, value in (("-tune", self.tune), ("-profile:v", self.profile), ("-level", self.level)):
            if value:
                args += [flag, value]
        args += ["-pix_fmt", "yuv420p10le" if self.bit_depth == 10 else "yuv420p"]
        if self.fps:
            args += ["-r", self.fps]
        return args

    def container_args(self) -> List[str]:
        """@return the options of the container (MP4 tag and fast start)"""
        if self.container != "mp4":
            return []
        args = ["-tag:v", "hvc1"] if self.video == "x265" else []
        return args + (["-movflags", "+faststart"] if self.faststart else [])

    def audio_args(self, mixed: bool) -> List[str]:
        """
        @param mixed  True if the project has sound (an audio label to map, or a source to copy)
        @return       encoder, bitrate, channels and sample rate; "-c:a copy" for COPY_AUDIO; just -an without sound
        """
        if not mixed or self.audio == NO_AUDIO:
            return ["-an"]
        if self.audio == COPY_AUDIO:
            return ["-c:a", "copy"]
        a = AUDIO[self.audio]
        args = ["-c:a", a.encoder]
        if a.bitrates:
            args += ["-b:a", "%dk" % self.audio_bitrate]
        if self.channels:
            args += ["-ac", str(self.channels)]
        if self.sample_rate:
            args += ["-ar", str(self.sample_rate)]
        return args


# --------------------------------------------------------------------------- #
# Built-in presets
# --------------------------------------------------------------------------- #

PRESETS: Dict[str, ExportOptions] = {
    "web_h264": ExportOptions(),
    "phone_720": ExportOptions(crf=23, speed="fast", height=720, audio_bitrate=128),
    "small_h265": ExportOptions(video="x265", crf=25, audio_bitrate=160),
    "web_vp9": ExportOptions(container="webm", video="vp9", crf=31, audio="opus"),
    "archive": ExportOptions(container="mkv", crf=16, speed="slow", audio="flac", audio_bitrate=0),
    "web_av1": ExportOptions(container="mkv", video="svtav1", crf=32, speed="8", audio="opus", audio_bitrate=128),
}
"""Presets of the export dialog; language entry \"export.preset.<key>\" names each."""


def available_presets(encoders: Optional[AbstractSet[str]] = None) -> Dict[str, ExportOptions]:
    """@return the presets whose encoders are all in `encoders`"""
    return {k: o for k, o in PRESETS.items() if o.normalized(encoders) == o}
