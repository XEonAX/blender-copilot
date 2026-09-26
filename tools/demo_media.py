#!/usr/bin/env python3
"""Compose the captured strip into the pictures the README carries.

    python3 tools/demo_media.py

`tools/demo_capture.py` writes a strip of raw frames under `logs/demo/raw/`;
`logs/` is gitignored, which is correct - it is scratch - so nothing a reader
should see can live there. This turns the strip into the handful of files under
`docs/media/` that the README links, and writes nothing else.

It runs on plain CPython (Pillow is present on this machine and in Blender's own
Python; nothing here needs `bpy`, so the composition is testable without
launching Blender). `ffmpeg` is optional: it is used for the MP4 that the social
post wants, and its absence is reported rather than fatal.

Two judgement calls are recorded here rather than in a comment somewhere else:

* **The panel's rectangle is read out of the capture log**, not assumed from the
  window size and not guessed from the pixels. `--window-geometry` is a request
  the window manager is free to refuse - a run that asked for 1400x1010 got an
  area of 1145x827 - so a crop composed against the requested width puts the
  panel half off the edge of the picture. The pixels were tried first and were
  not good enough: the largest column-to-column change in the right half of the
  frame is the boundary between the panel's background and the vertical tab
  strip *inside* the sidebar, not the sidebar's left edge, and the first
  composed receipt was a 24 px sliver. `demo_capture.py` writes the region's own
  rect into `logs/demo-capture.txt`; `panel_rect()` reads it there, and only
  falls back to the pixel step when the log is missing.
* **One palette for every GIF frame**, taken from the last frame, and dithered. Per-
  frame adaptive palettes are a few percent smaller and flicker: the same grey viewport
  shifts shade between frames wherever the text happens to change how the colours are
  allocated.
* **Both a GIF and an MP4**, because on GitHub they do different jobs and only one of
  them can do the job people expect. A README cannot inline an MP4 - the markdown
  sanitiser strips `<video>` and `<img>` will not play one - so the GIF is what
  animates in place, and the MP4 is the full-colour, much smaller copy to link and to
  post elsewhere.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# All three are overridable so this can be dry-run against a strip that is not the
# current one - a kept partial recording, say - without writing over the published
# assets. `DEMO_RAW` names the frames, `DEMO_LOG` the capture log the panel rectangle
# is read from, `DEMO_OUT` where the results go.
RAW = Path(os.environ.get("DEMO_RAW") or ROOT / "logs" / "demo" / "raw")
LOG = Path(os.environ.get("DEMO_LOG") or ROOT / "logs" / "demo-capture.txt")
OUT = Path(os.environ.get("DEMO_OUT") or ROOT / "docs" / "media")

GIF_WIDTH = 900
FRAME_MS = 120
GIF_COLOURS = 128
# Floyd-Steinberg, because a dark viewport with a lit ship in it is a gradient, and an
# undithered 128-colour palette bands it into rings. A few per cent of file size for
# the difference between "a GIF" and "the screenshot, moving".
DITHER = Image.FLOYDSTEINBERG
# A frame is dropped when less than this percentage of its pixels moved since the
# last one kept. The numbers behind the threshold: the elapsed-seconds digits in the
# status row move about 0.1% of a frame, a streaming sentence about 2%, and a ship
# crossing the viewport far more. A live turn spends most of its time waiting for the
# model, so without this the GIF is mostly a ticking clock.
PRUNE_PERCENT = 0.4
FILMSTRIP_TILES = 6
FILMSTRIP_TILE = 240
# The build is paced, not timed: its frames are *events* (one each time something
# changed), so how fast to play them is a presentation choice. The flight is not - its
# frames are every frame the model keyframed, so it is played at the scene's own rate
# and comes out at true speed. One rate for both is what this did first, and it crawled
# through the flight while racing the build.
MP4_BUILD_SECONDS = 12
MP4_FPS_FLOOR = 10
MP4_FPS_CEILING = 30
SCENE_FPS = re.compile(r"scene fps: (\d+)")
# Flight frames going into the GIF: 1 in 4. With a dense sweep the GIF would otherwise
# blow past GitHub's 10 MiB inline ceiling, and the GIF is the format that must stay
# small - the MP4 is where motion is actually visible.
GIF_SWEEP_STRIDE = 4
# And the build frames: 1 in 2. The first five-turn composition came out at 8.4 MiB and
# 24 seconds - legal, but heavy for the first thing a reader loads and long for a
# loop. Dropping every other build frame keeps the text advancing visibly per step
# while halving the weight, which is the difference between a hero image and a
# download.
GIF_BUILD_STRIDE = 2

RECT = re.compile(r"panel rect x=(\d+) y=(\d+) w=(\d+) h=(\d+)")


def panel_rect(image: Image.Image) -> tuple[int, int]:
    """`(left, top)` of the sidebar inside the frame: measured if possible.

    The log is the capture's own account of where its panel was, so it cannot
    drift from the frames it describes. The pixel step below is the fallback for
    a strip captured before that line existed.
    """
    if LOG.exists():
        found = RECT.findall(LOG.read_text(encoding="utf-8"))
        if found:
            left, top, _, _ = (int(value) for value in found[-1])
            if 0 < left < image.width and 0 <= top < image.height:
                return left, top
            print(f"MEDIA | log says panel x={left} y={top}, outside the frame; "
                  "falling back to the pixels")
    grey = image.convert("L")
    width, height = grey.size
    rows = range(0, height, max(1, height // 64))
    columns = [sum(grey.getpixel((x, y)) for y in rows) / len(rows) for x in range(width)]
    changes = [(abs(columns[x] - columns[x - 1]), x) for x in range(width // 2, width)]
    if changes and max(changes)[0] >= 4:
        print("MEDIA | no capture log; panel edge taken from the pixels")
        return max(changes)[1], 0
    print("MEDIA | no capture log and no edge found; using 64% of the width")
    return int(width * 0.64), 0


def scene_fps() -> int:
    """The rate the animation was authored at, read from the capture log.

    `demo_capture.py` records it after the sweep. The fallback is 24 - Blender's default
    and what this recording was made at - and it announces itself, because a playback
    rate that is a guess would silently misrepresent how smooth the model's curve is.
    """
    if LOG.exists():
        found = SCENE_FPS.findall(LOG.read_text(encoding="utf-8"))
        if found:
            return int(found[-1])
    print("MEDIA | no 'scene fps' in the capture log; assuming Blender's default 24")
    return 24


def encode(frames: list[Path], staging: Path) -> None:
    """Numbered symlinks for one segment.

    Symlinks because ffmpeg's image-sequence input reads a directory in name order, and
    these frames are named for what they are rather than for where they go.
    """
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for index, frame in enumerate(frames, start=1):
        (staging / f"{index:04d}.png").symlink_to(frame.resolve())


def mp4(build: list[Path], flight: list[Path], target: Path) -> str:
    """The video: the build at a readable pace, then the animation played at its own.

    Two inputs, two rates, and **the concat filter** rather than `-c copy` on two
    encoded segments. That last one was a real bug: joining segments by copying streams
    cannot represent two different frame rates, and the file that came out was a single
    15 fps stream - so the flight, which is the part that is *about* motion, was playing
    slower than it was authored and looked exactly as steppy as before. The concat
    filter re-encodes and keeps each segment's timing.
    """
    if shutil.which("ffmpeg") is None:
        return "ffmpeg not on PATH - skipped"
    work = target.parent / ".mp4-staging"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    build_rate = max(
        MP4_FPS_FLOOR, min(MP4_FPS_CEILING, round(len(build) / MP4_BUILD_SECONDS))
    ) if build else 0
    flight_rate = scene_fps() if flight else 0
    if not build and not flight:
        shutil.rmtree(work, ignore_errors=True)
        return "no frames to encode"

    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    plan: list[tuple[list[Path], int, str, str]] = []
    if build:
        plan.append((build, build_rate, "build", "b"))
    if flight:
        plan.append((flight, flight_rate, "flight", "f"))
    for index, (frames, rate, name, label) in enumerate(plan):
        encode(frames, work / f"{name}-frames")
        inputs += ["-framerate", str(rate), "-i", str(work / f"{name}-frames" / "%04d.png")]
        chains.append(
            f"[{index}:v]scale={GIF_WIDTH}:-2:flags=lanczos,setpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
        print(
            f"MEDIA | {name}: {len(frames)} frames at {rate} fps = {len(frames) / rate:.1f}s"
        )

    if len(plan) == 1:
        filter_complex = ",".join(chains) + f";{labels[0]}null[out]"
    else:
        filter_complex = ",".join(chains) + f";{''.join(labels)}concat=n={len(labels)}:v=1:a=0[out]"
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return f"ffmpeg failed: {lines[-1] if lines else 'no output'}"
    return ""


def prune_static(frames: list[Path], threshold: float = PRUNE_PERCENT) -> list[Path]:
    """Drop the frames where nothing happened.

    A live turn is mostly *thinking*: no reply is arriving, and the only thing moving
    on screen is the counter in the status row. Those frames are real - the wait is
    real - but a GIF made of them is a GIF of a ticking clock, so each frame is
    compared against the last one kept and dropped when less than `threshold` per
    cent of its pixels changed. Comparing against the *kept* frame rather than the
    previous one is what stops a slow drift from being pruned away one 0.3% step at a
    time.
    """
    small = (160, 100)
    kept: list[Path] = []
    previous: Image.Image | None = None
    for path in frames:
        current = Image.open(path).convert("L").resize(small)
        if previous is not None:
            now, before = current.load(), previous.load()
            moved = sum(
                1
                for y in range(small[1])
                for x in range(small[0])
                if abs(now[x, y] - before[x, y]) > 6
            )
            if 100.0 * moved / (small[0] * small[1]) < threshold:
                continue
        kept.append(path)
        previous = current
    return kept


def filmstrip(frames: list[Path], tiles: int = FILMSTRIP_TILES) -> Image.Image | None:
    """Six poses from the animation, side by side.

    A GIF loops, so a reader who looks away for ten seconds misses the flight. A strip
    of stills is the version that survives being glanced at, and it is the only
    picture here that shows the *shape* of the motion rather than a moment of it.
    """
    sweep = [frame for frame in frames if "sweep" in frame.name]
    if len(sweep) < 2:
        return None
    picks = [
        sweep[round(index * (len(sweep) - 1) / (tiles - 1))] for index in range(tiles)
    ]
    gap = 6
    images = []
    for path in picks:
        image = Image.open(path).convert("RGB")
        height = round(FILMSTRIP_TILE * image.height / image.width)
        images.append(image.resize((FILMSTRIP_TILE, height), Image.LANCZOS))
    height = max(image.height for image in images)
    width = sum(image.width for image in images) + gap * (len(images) - 1)
    strip = Image.new("RGB", (width, height), (18, 18, 20))
    x = 0
    for image in images:
        strip.paste(image, (x, 0))
        x += image.width + gap
    return strip


def shown(path: Path) -> str:
    """The path as it should be printed: relative when it is inside the repo.

    `path.relative_to(ROOT)` raises when `DEMO_OUT` points somewhere else - which is
    the whole point of that variable, and it killed the first dry run at the last line
    of the report, after every asset had been written successfully.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    frames = sorted(RAW.glob("f*.png"))
    if not frames:
        print(f"MEDIA | no frames in {RAW} - run tools/demo_capture.py first")
        print("MEDIA FAILED", flush=True)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    written: list[tuple[Path, str]] = []
    final = Image.open(frames[-1]).convert("RGB")
    width, height = final.size
    left, top = panel_rect(final)
    print(f"MEDIA | {len(frames)} frames, {width}x{height} px, panel column {left}-{width}")
    kept = prune_static([frame for frame in frames if "sweep" not in frame.name])
    flight = [frame for frame in frames if "sweep" in frame.name]
    # Flight frames are never pruned, and that is a fix rather than a preference: on the
    # spaceship take the pruner silently dropped 86 of 241 of them. Its threshold is
    # tuned for the *panel*, where a sentence arriving changes a lot of pixels - but the
    # whole frame is downscaled to 160x100 to measure that, and at that size a ship
    # moving smoothly changes fewer pixels than the threshold. A continuous animation is
    # all events; a reading of "nothing happened" between two of its frames is the
    # measurement failing, not the animation.
    kept = sorted(kept + flight, key=lambda path: path.name)
    print(
        f"MEDIA | dropped {len(frames) - len(kept)} still build frames; "
        f"{len(kept)} kept ({len(flight)} of them flight)"
    )
    # Order is preserved: a frame is dropped or kept in place, never moved.
    gif_frames: list[Path] = []
    flight_seen = 0
    build_seen = 0
    for frame in kept:
        if "sweep" in frame.name:
            if flight_seen % GIF_SWEEP_STRIDE == 0:
                gif_frames.append(frame)
            flight_seen += 1
        else:
            if build_seen % GIF_BUILD_STRIDE == 0:
                gif_frames.append(frame)
            build_seen += 1
    print(
        f"MEDIA | the gif gets {len(gif_frames)} of them "
        f"(build 1 in {GIF_BUILD_STRIDE}, flight 1 in {GIF_SWEEP_STRIDE}); "
        f"the mp4 gets all {len(kept)}"
    )

    # 1. The hero: the whole frame, unobstructed - the panel *inside* Blender,
    #    next to what the turn built.
    hero = OUT / "hero.png"
    final.save(hero, optimize=True)
    written.append((hero, f"{width}x{height}"))

    # 2. The receipt, at the size the panel really is, for the README's undo
    #    claim: crop only, no resampling, so nothing is invented.
    receipt = OUT / "receipt.png"
    final.crop((left, top, width, height)).save(receipt, optimize=True)
    written.append((receipt, f"{width - left}x{height - top}"))

    # 3. A mid-turn frame, where the transcript shows the tool row, the code
    #    identity row and the output box at once.
    mid = frames[int(len(frames) * 0.45)]
    midshot = OUT / "tool-row.png"
    Image.open(mid).convert("RGB").crop((left, top, width, height)).save(
        midshot, optimize=True
    )
    written.append((midshot, f"{width - left}x{height - top} from {mid.name}"))

    # 4. The animation.
    palette = final.quantize(colors=GIF_COLOURS, method=Image.MEDIANCUT)
    pictures = []
    for frame in gif_frames:
        image = Image.open(frame).convert("RGB")
        if image.width != GIF_WIDTH:
            image = image.resize(
                (GIF_WIDTH, round(image.height * GIF_WIDTH / image.width)),
                Image.LANCZOS,
            )
        pictures.append(image.quantize(palette=palette, dither=DITHER))
    gif = OUT / "turn.gif"
    pictures[0].save(
        gif,
        save_all=True,
        append_images=pictures[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
    )
    written.append((gif, f"{GIF_WIDTH}x{pictures[0].height}, {len(pictures)} frames"))
    print(
        f"MEDIA | the gif runs {len(pictures) * FRAME_MS / 1000:.1f}s "
        f"({len(pictures)} frames at {FRAME_MS}ms, still ones already dropped)"
    )

    # 5. The flight, as stills, for a reader who looks away during the loop.
    strip = filmstrip(kept)
    if strip is not None:
        flight = OUT / "flight.png"
        strip.save(flight, optimize=True)
        written.append((flight, f"{strip.width}x{strip.height}, {FILMSTRIP_TILES} poses"))
    else:
        print("MEDIA | no sweep frames in the strip - no flight filmstrip")

    build_frames = [frame for frame in kept if "sweep" not in frame.name]
    flight_frames = [frame for frame in kept if "sweep" in frame.name]
    problems = mp4(build_frames, flight_frames, OUT / "turn.mp4")
    if not problems:
        written.append(
            (
                OUT / "turn.mp4",
                f"{GIF_WIDTH}px, {len(build_frames)} build frames + "
                f"{len(flight_frames)} flight frames at {scene_fps()} fps",
            )
        )
    else:
        print(f"MEDIA | {problems}")

    for path, detail in written:
        if not path.exists() or path.stat().st_size == 0:
            print(f"MEDIA | FAILED: {path} was not written")
            print("MEDIA FAILED", flush=True)
            return 1
        size = path.stat().st_size
        print(f"MEDIA | wrote {shown(path)} - {detail}, {size / 1024:.0f} KiB")

    gif_size = (OUT / "turn.gif").stat().st_size / 1024 / 1024
    verdict = "MEDIA OK" if gif_size < 8 else "MEDIA OK (large gif)"
    print(f"MEDIA | {verdict} - gif is {gif_size:.2f} MiB, GitHub inline limit is 10 MiB")
    print(verdict, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
