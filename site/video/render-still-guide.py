"""Render honestly labelled screenshot walkthroughs from an editable JSON timeline.

Usage: uv run --with pillow render-still-guide.py timeline.json raw-dir output-dir
Requires FFmpeg on PATH. Originals are read-only; all crops and frames are derivatives.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


def crop_source(raw: Path, shot: dict[str, Any]) -> Image.Image:
    """Read a genuine screenshot and enforce an in-bounds editorial crop."""
    with Image.open(raw / shot["source"]) as original:
        x, y, width, height = shot["crop"]
        if min(x, y) < 0 or min(width, height) <= 0:
            raise ValueError(f"Invalid crop: {shot}")
        if x + width > original.width or y + height > original.height:
            raise ValueError(f"Crop exceeds source: {shot}")
        return original.convert("RGB").crop((x, y, x + width, y + height))


def timestamp(seconds: float) -> str:
    """Return a WebVTT timestamp without rounding drift."""
    millis = round(seconds * 1000)
    return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02}.{millis % 1000:03}"


def render(spec: dict[str, Any], raw: Path, output: Path, ffmpeg: str) -> None:
    """Render still holds and intentional cuts; never synthesize interaction motion."""
    output.mkdir(parents=True, exist_ok=True)
    width, height = spec["size"]
    font = ImageFont.truetype(spec["font"], 34)
    small = ImageFont.truetype(spec["font"], 23)
    for still in spec["stills"]:
        crop_source(raw, still).save(output / f"{still['name']}.jpg", quality=95, subsampling=0)
    for clip in spec["clips"]:
        frames = output / "frames" / clip["name"]
        frames.mkdir(parents=True, exist_ok=True)
        concat: list[str] = []
        captions = ["WEBVTT", ""]
        elapsed = 0.0
        for index, shot in enumerate(clip["shots"]):
            frame = Image.new("RGB", (width, height), spec["background"])
            fitted = ImageOps.contain(crop_source(raw, shot), (width - 48, height - 190))
            frame.paste(
                fitted, ((width - fitted.width) // 2, 42 + (height - 190 - fitted.height) // 2)
            )
            draw = ImageDraw.Draw(frame)
            draw.text((24, 9), "CLIO  /  SCREENSHOT WALKTHROUGH", font=small, fill="#4b636d")
            draw.rectangle((0, height - 130, width, height), fill="#ffffff")
            draw.line((0, height - 130, width, height - 130), fill="#c9dce3", width=2)
            lines = textwrap.wrap(shot["caption"], width=62)
            if len(lines) > 2:
                raise ValueError("Caption exceeds two readable lines")
            draw.multiline_text(
                (28, height - 113), "\n".join(lines), font=font, fill="#173b45", spacing=8
            )
            path = frames / f"{index:02}.png"
            frame.save(path)
            if index == 0:
                frame.save(output / f"{clip['name']}.jpg", quality=95, subsampling=0)
            concat.extend([f"file '{path.name}'", f"duration {shot['seconds']}"])
            end = elapsed + shot["seconds"]
            captions.extend([f"{timestamp(elapsed)} --> {timestamp(end)}", shot["caption"], ""])
            elapsed = end
        concat.append(f"file '{len(clip['shots']) - 1:02}.png'")
        playlist = frames / "frames.ffconcat"
        playlist.write_text("\n".join(concat), encoding="utf-8")
        (output / f"{clip['name']}.vtt").write_text("\n".join(captions), encoding="utf-8")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(playlist),
                "-t",
                str(elapsed),
                "-vf",
                f"fps={spec['fps']},format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                "-an",
                str(output / f"{clip['name']}.mp4"),
            ],
            check=True,
        )
        print(f"Rendered {clip['name']}: {elapsed:g}s", flush=True)


def main() -> None:
    """Parse reproducible inputs and render the complete set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timeline", type=Path)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("FFmpeg is required; pass --ffmpeg with its executable path")
    render(
        json.loads(args.timeline.read_text(encoding="utf-8")), args.raw, args.output, args.ffmpeg
    )


if __name__ == "__main__":
    main()
