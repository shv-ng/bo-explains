#!/usr/bin/env python3
"""
make_short.py — Ink Explainer Short pipeline runner.

AI writes project.json once. This script does everything else.

Usage:
  python make_short.py project.json                 # run all steps (no image gen)
  python make_short.py project.json --agy           # run all, auto-generate missing images
  python make_short.py project.json --steps voice,shots,assemble,captions
  python make_short.py project.json --skip captions

── Shot timing ─────────────────────────────────────────────────────────────

  Shot durations are derived directly from voice/voice.wav: total audio
  duration is split across shots in proportion to each shot's token count.
  No transcription API call, no out/words.json.

  Captions are cut from the same shots.json timings, so captions and image
  cuts are always in sync by construction — each shot's phrase is chunked
  into groups of `caption_group_size` words and spread evenly across that
  shot's start→end span.

── Image workflow ──────────────────────────────────────────────────────────────

  Every image must end up at img/shots_NNN.jpg, and every img/ file must have
  passed through the watermark remover (gwr). The invariant is:

      raw/N.jpg (or .png)  ──gwr──►  img/shots_NNN.jpg

  Two ways raw/N.jpg comes to exist:

    1. MANUAL (default): run `--steps prompts`, copy-paste prompts from
       raw/prompts.md into Gemini web, save downloads as raw/1.jpg, raw/2.jpg …
       Then run the pipeline (no --agy). The `watermark` step converts each
       raw/N.jpg → img/shots_NNN.jpg.

    2. AUTO (--agy): the `images` step generates any MISSING shot via
       `npx imagine-agy` into raw/N.jpg (loop-last-shot is skipped).
       The `watermark` step then converts raw → img as usual.

  If --agy is NOT passed and some img/shots_NNN.jpg are missing, the `images`
  step logs the missing shots and writes prompts for them to raw/prompts.md
  (same as `--steps prompts`). Nothing else fails — but `assemble` will.

── Watermark removal ───────────────────────────────────────────────────────────
  Uses: npx -p sharp -p @pilio/gemini-watermark-remover gwr remove in.jpg --output out.png
  The `watermark` step ALWAYS reads raw/N.jpg and writes img/shots_NNN.jpg.
  Skip with --skip watermark (not recommended).

── Steps ───────────────────────────────────────────────────────────────────────
  prompts   — write raw/prompts.md for shots whose img/ is still missing
  voice     — generate voice/voice.wav via Gemini TTS
  images    — if --agy: generate missing shots via imagine-agy into raw/N.jpg
              if not:   report missing shots + write raw/prompts.md
  watermark — gwr raw/N.jpg → img/shots_NNN.jpg (also handles loop-last-shot)
  shots     — build shots.json timing (proportional to phrase token count)
  assemble  — build out/final.mp4
  captions  — build captions + burn final_captioned.mp4 (uses shots.json)

── project.json schema ─────────────────────────────────────────────────────────
{
  "name": "kebab-case-name",
  "narration": "Full spoken text (English or Hinglish)...",
  "shots": [
    {
      "phrase": "exact narration chunk for this shot",
      "scene": "2-4 stick figures doing something specific"
    }
  ],
  "style": {
    "clothing": "plain casual t-shirts and simple trousers",
    "background": "minimal cozy bedroom, single bed with white sheets, one window",
    "sky": "soft warm indoor light, pale cream ceiling, no outdoor sky"
  },
  "voice": {
    "voice_name": "Puck",
    "style": "deep male narrator, calm, slow, unhurried, contemplative",
    "model": "gemini-3.8-flash-tts"
  },
  "image_model": "gemini-3.8-flash-low",
  "fps": 30,
  "loop_last_shot": true,
  "caption_group_size": 3
}

  Notes:
    - voice.target_duration is IGNORED if present. Natural pace only.
      Forcing atempo stretches the audio unnaturally and is not needed —
      timing is measured from the actual audio, not from a target.
    - caption_text is IGNORED. Captions are derived from each shot's
      `phrase` field, which is what the narration is built from anyway.
    - shot phrases MUST concatenate exactly (word-by-word) to narration.
      `step_shots` validates this and refuses to run if they diverge.

Requirements:
  GEMINI_API_KEY env var (for voice)
  ffmpeg, ffprobe on PATH
  npx on PATH (for watermark removal; and imagine-agy if --agy)
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


# ── locked style prefix ──────────────────────────────────────────────────────
STYLE_PREFIX = (
    "Generate exactly 1 image using your generate_image tool. "
    "2D flat cartoon vector illustration in the style of a stick-figure history "
    "explainer YouTube channel, wide shot. Small full-body characters standing in "
    "the middle ground, shown head to toe with feet visible, never a close-up. "
    "Character style: simple stick-figure people with a large plain pale egg-shaped "
    "head with flat white fill and no shine or shading; two tiny black dot eyes, a "
    "tiny simple line mouth, expressive eyebrows only when needed; thin noodle-like "
    "arms and legs, small mitten hands, thin uniform black outline, completely flat "
    "color fills. Hair: most characters have a small messy tuft of 5 to 8 thin "
    "scribbly dark-brown strands sticking up from the top of the head, drawn as "
    "loose wavy ink lines; women may have long straight dark hair past the shoulders; "
    "old men have a white beard and a bald head. Characters differ in height, age and "
    "body shape, each with a different facial expression. Clothing: {clothing}. "
    "Background: {background}. Sky: {sky}. Background is flat vector with simple "
    "shapes, soft depth, no vignette, no gradients on characters. Rich, saturated, "
    "vibrant colors. Characters are clearly separated from the background. "
    "Scene: {scene}. No text, no captions, no letters, no watermark. "
    "Avoid: no close-up, no portrait, no vignette, no dark lighting, no gradients "
    "on characters, no shading, no glossy highlights, no painterly texture, no "
    "detailed face, no realistic anatomy, no 3D, no Pixar, no Disney, no anime, "
    "no bald characters (except old men), no identical faces, no identical bodies, "
    "no solid helmet-like hair, no vintage paper, no crosshatching, no text. "
    "Aspect ratio: 9:16 (vertical, 1080×1920). No text, no watermark, no signature. "
)

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,68,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,70,70,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ── helpers ──────────────────────────────────────────────────────────────────


def run(cmd, check=True, capture=False):
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        err = (r.stderr or "")[-2000:]
        raise SystemExit(
            f"Command failed: {' '.join(str(c) for c in cmd[:6])} ...\n{err}"
        )
    return r


def ffprobe_duration(path):
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        capture=True,
    )
    return float(r.stdout.strip())


def norm(s):
    """Lowercase word tokens, latin+digits only."""
    return re.findall(r"[a-z0-9']+", s.lower())


def display_tokens(s):
    """Preserve original case, drop punctuation. Used for caption display."""
    return [t for t in re.findall(r"[A-Za-z\u0900-\u097F0-9₹'’\-]+", s)]


def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_prompt(style, scene):
    return STYLE_PREFIX.format(
        clothing=style["clothing"],
        background=style["background"],
        sky=style["sky"],
        scene=scene,
    )


def is_valid_image(path, min_bytes=5000):
    return os.path.exists(path) and os.path.getsize(path) > min_bytes


def raw_source_for(base_dir, i):
    """Return path to raw/N.jpg or raw/N.png for shot index i, or None."""
    raw_dir = os.path.join(base_dir, "raw")
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = os.path.join(raw_dir, f"{i}{ext}")
        if os.path.exists(p):
            return p
    return None


def missing_shots(proj, base_dir):
    """
    Return list of (index, shot_dict) for shots whose img/shots_NNN.jpg
    is missing OR has no raw/N.* source (in which case the img/ would
    fail watermarking anyway).

    Loop-last-shot is excluded — it's copied from shots_001.jpg by `watermark`.
    """
    shots = proj["shots"]
    loop_last = proj.get("loop_last_shot", True)
    total = len(shots)
    img_dir = os.path.join(base_dir, "img")
    out = []
    for i, shot in enumerate(shots, start=1):
        if loop_last and i == total:
            continue
        dest = os.path.join(img_dir, f"shots_{i:03d}.jpg")
        if not is_valid_image(dest):
            out.append((i, shot))
    return out


# ── step: prompts (write raw/prompts.md for missing shots) ───────────────────


def step_prompts(proj, base_dir):
    raw_dir = os.path.join(base_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    out_path = os.path.join(raw_dir, "prompts.md")

    shots = proj["shots"]
    style = proj["style"]
    total = len(shots)
    todo = missing_shots(proj, base_dir)

    if not todo:
        print(
            f"[prompts] all {total} shots already have img/shots_NNN.jpg — nothing to write"
        )
        if os.path.exists(out_path):
            os.remove(out_path)
            print(f"[prompts] removed stale {out_path}")
        return

    lines = [
        f"# {proj.get('name', 'short')} — image prompts\n",
        f"Missing shots: {len(todo)} of {total}\n",
        "Save images as: raw/<N>.jpg (N = shot number)\n",
        "Aspect ratio: 9:16 (vertical)\n",
        "---\n",
    ]

    for i, shot in todo:
        prompt = build_prompt(style, shot["scene"])
        lines.append(f"## Shot #{i}\n")
        lines.append(f"{prompt}\n\n")
        lines.append("---\n")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"[prompts] wrote {out_path} ({len(todo)} missing shot(s))")
    print("[prompts] save images as raw/<N>.jpg, then re-run watermark")


# ── step: images (report-only, or imagine-agy if --agy) ─────────────────────


def step_images(proj, base_dir, use_agy=False):
    todo = missing_shots(proj, base_dir)

    if not todo:
        print("[images] all shots present — nothing to do")
        return

    if not use_agy:
        print(f"[images] {len(todo)} shot(s) missing and --agy not passed")
        for i, shot in todo:
            print(
                f"[images]   missing: shots_{i:03d}.jpg  (scene: {shot['scene'][:60]}…)"
            )
        step_prompts(proj, base_dir)
        return

    raw_dir = os.path.join(base_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    style = proj["style"]
    image_model = proj.get("image_model", "gemini-3.8-flash-low")

    for i, shot in todo:
        existing = raw_source_for(base_dir, i)
        if existing:
            print(
                f"[images] shot {i:02d} — raw source already present "
                f"({os.path.basename(existing)}), skipping generation"
            )
            continue

        dest = os.path.join(raw_dir, f"{i}.jpg")
        prompt = build_prompt(style, shot["scene"])
        print(f"[images] shot {i:02d} — generating via imagine-agy …")

        max_retries = 3
        ok = False
        for attempt in range(max_retries):
            result = subprocess.run(
                [
                    "agy",
                    "--model",
                    image_model,
                    "--print",
                    f"{prompt}. Save it as {dest} in 9:16 vertical aspect ratio",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and is_valid_image(dest):
                print(f"[images] shot {i:02d} — done → raw/{i}.jpg")
                ok = True
                break
            wait = 45 * (attempt + 1)
            print(f"[images] attempt {attempt + 1} failed, retrying in {wait}s …")
            if os.path.exists(dest):
                os.remove(dest)
            if attempt < max_retries - 1:
                time.sleep(wait)
        if not ok:
            raise SystemExit(
                f"Failed to generate shot {i} after {max_retries} attempts"
            )

    remaining = missing_shots(proj, base_dir)
    if remaining:
        still = [i for i, _ in remaining]
        print(f"[images] WARNING: shots still missing after generation: {still}")
        print("[images] run --steps watermark to promote raw → img")


# ── step: watermark removal (raw → img, always) ─────────────────────────────


def step_watermark(proj, base_dir):
    shots = proj["shots"]
    img_dir = os.path.join(base_dir, "img")
    os.makedirs(img_dir, exist_ok=True)

    loop_last = proj.get("loop_last_shot", True)
    total = len(shots)
    missing_raw = []

    for i in range(1, total + 1):
        dest = os.path.join(img_dir, f"shots_{i:03d}.jpg")

        if loop_last and i == total:
            first = os.path.join(img_dir, "shots_001.jpg")
            if not is_valid_image(first):
                print(
                    f"[watermark] shot {i:02d}/{total} — shots_001.jpg not ready, "
                    "skipping loop copy"
                )
                continue
            if is_valid_image(dest):
                print(f"[watermark] shot {i:02d}/{total} — loop: skipping (exists)")
                continue
            shutil.copy2(first, dest)
            print(f"[watermark] shot {i:02d}/{total} — loop: copied shots_001.jpg")
            continue

        if is_valid_image(dest):
            print(f"[watermark] shot {i:02d}/{total} — skipping (exists)")
            continue

        src = raw_source_for(base_dir, i)
        if not src:
            print(f"[watermark] shot {i:02d}/{total} — MISSING raw/{i}.jpg|.png")
            missing_raw.append(i)
            continue

        tmp_out = dest + ".tmp.png"
        print(
            f"[watermark] shot {i:02d}/{total} — removing watermark from "
            f"{os.path.basename(src)} …"
        )
        cmd = [
            "npx",
            "--yes",
            "-p",
            "sharp",
            "-p",
            "@pilio/gemini-watermark-remover",
            "gwr",
            "remove",
            src,
            "--output",
            tmp_out,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if (
            result.returncode != 0
            or not os.path.exists(tmp_out)
            or os.path.getsize(tmp_out) < 1000
        ):
            print(
                f"[watermark] shot {i:02d}/{total} — gwr failed, copying original "
                "without removal"
            )
            print(f"  stderr: {result.stderr[-300:]}")
            shutil.copy2(src, dest)
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            continue

        run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_out, dest])
        os.remove(tmp_out)
        print(f"[watermark] shot {i:02d}/{total} — done → {os.path.basename(dest)}")

    if missing_raw:
        print(
            f"[watermark] {len(missing_raw)} shot(s) had no raw source: {missing_raw}"
        )
        print("[watermark] run --steps prompts to write raw/prompts.md for these")


# ── step: voice ───────────────────────────────────────────────────────────────


def step_voice(proj, base_dir):
    out_dir = os.path.join(base_dir, "voice")
    out_path = os.path.join(out_dir, "voice.wav")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print(f"[voice] skipping — {out_path} already exists")
        print("[voice] (delete it to regenerate; timing is measured from this file)")
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set")

    vcfg = proj.get("voice", {})
    voice_name = vcfg.get("voice_name", "Puck")
    style = vcfg.get(
        "style", "deep male narrator, calm, slow, unhurried, contemplative"
    )
    model = vcfg.get("model", "gemini-3.8-flash-tts")
    # NOTE: voice.target_duration is intentionally ignored. Forcing atempo
    # stretches the audio unnaturally; timing is derived from the resulting
    # voice.wav by splitting proportionally across shots, so there's no need
    # to force a specific length.
    text = re.sub(r"\s+", " ", proj["narration"]).strip()

    os.makedirs(out_dir, exist_ok=True)
    resp_path = os.path.join(out_dir, "_tts_response.json")
    raw_path = os.path.join(out_dir, "_tts_raw.wav")

    payload = {
        "model": model,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [{"type": "speech_metadata", "style": style}],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice_name}]},
    }

    print(f"[voice] calling Gemini TTS ({model}, voice={voice_name}) …")
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise SystemExit(f"Gemini TTS HTTP {e.code}: {body[:800]}")

    with open(resp_path, "w") as f:
        json.dump(resp_data, f)

    audio_data = None
    for step in resp_data.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "audio":
                audio_data = block.get("data")
    if not audio_data:
        raise SystemExit(
            "No audio data in Gemini TTS response — check voice/_tts_response.json"
        )

    with open(raw_path, "wb") as f:
        f.write(base64.b64decode(audio_data))

    raw_dur = ffprobe_duration(raw_path)
    print(f"[voice] raw audio: {raw_dur:.2f}s — resampling to 48 kHz mono")

    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            raw_path,
            "-ar",
            "48000",
            "-ac",
            "1",
            out_path,
        ]
    )

    dur = ffprobe_duration(out_path)
    words = len(text.split())
    print(
        f"[voice] wrote {out_path} ({dur:.2f}s, {words} words, {words / dur:.2f} w/s)"
    )


# ── step: shots.json ──────────────────────────────────────────────────────────


def step_shots(proj, base_dir):
    voice_path = os.path.join(base_dir, "voice", "voice.wav")
    shots_json_path = os.path.join(base_dir, "shots.json")

    if not os.path.exists(voice_path):
        raise SystemExit("voice/voice.wav not found — run the 'voice' step first")
    duration = ffprobe_duration(voice_path)
    print(f"[shots] audio duration: {duration:.3f}s")

    shots = proj["shots"]

    # ── validate shot phrases concatenate to narration ──
    nar_tokens = norm(proj["narration"])
    shot_token_lists = [norm(s["phrase"]) for s in shots]
    flat = [w for tl in shot_token_lists for w in tl]
    if flat != nar_tokens:
        for i, (a, b) in enumerate(zip(flat, nar_tokens)):
            if a != b:
                raise SystemExit(
                    f"Mismatch at token {i}: shots say {a!r}, narration says {b!r}\n"
                    "Shot phrases must concatenate exactly to narration."
                )
        raise SystemExit(
            f"Token count mismatch: shots={len(flat)}, narration={len(nar_tokens)}"
        )

    total_tokens = sum(len(tl) for tl in shot_token_lists)
    if total_tokens == 0:
        raise SystemExit("all shot phrases are empty")

    result = []
    cum = 0.0
    n = len(shot_token_lists)
    for i, tl in enumerate(shot_token_lists):
        start = cum
        if i == n - 1:
            end = duration
        else:
            end = start + duration * (len(tl) / total_tokens)
        cum = end
        result.append(
            {
                "image": f"shots_{i + 1:03d}.jpg",
                "start": round(start, 3),
                "end": round(end, 3),
            }
        )

    with open(shots_json_path, "w") as f:
        json.dump(result, f, indent=2)

    durs = [s["end"] - s["start"] for s in result]
    print(f"[shots] wrote {shots_json_path}")
    print(
        f"[shots] {len(result)} shots, {duration:.2f}s, "
        f"avg hold {sum(durs) / len(durs):.2f}s, "
        f"min {min(durs):.2f}s, max {max(durs):.2f}s"
    )
    long = [(i + 1, round(d, 2)) for i, d in enumerate(durs) if d > 4.0]
    if long:
        print(f"[shots] WARN long holds (>4s): {long}")


# ── step: assemble ────────────────────────────────────────────────────────────


def step_assemble(proj, base_dir):
    shots_json_path = os.path.join(base_dir, "shots.json")
    img_dir = os.path.join(base_dir, "img")
    voice_path = os.path.join(base_dir, "voice", "voice.wav")
    out_dir = os.path.join(base_dir, "out")
    out_path = os.path.join(out_dir, "final.mp4")
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.exists(shots_json_path):
        raise SystemExit("shots.json not found — run the 'shots' step first")

    shots = json.load(open(shots_json_path))
    fps = str(proj.get("fps", 30))
    w, h = "1080", "1920"

    missing = [
        s["image"]
        for s in shots
        if not is_valid_image(os.path.join(img_dir, s["image"]))
    ]
    if missing:
        raise SystemExit(
            f"Missing images: {missing}\n"
            f"Run the 'watermark' step (after supplying raw/N.jpg or using --agy)."
        )

    tmp = tempfile.mkdtemp(prefix="ink_")
    parts = []
    print(f"[assemble] encoding {len(shots)} clips …")
    for i, s in enumerate(shots):
        img = os.path.join(img_dir, s["image"])
        dur = max(0.1, s["end"] - s["start"])
        out_clip = os.path.join(tmp, f"p{i:04d}.mp4")
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,"
            f"unsharp=3:3:0.5,"
            f"format=yuv420p"
        )
        run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-framerate",
                fps,
                "-i",
                img,
                "-t",
                f"{dur:.3f}",
                "-vf",
                vf,
                "-r",
                fps,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-an",
                out_clip,
            ]
        )
        parts.append(out_clip)

    concat_txt = os.path.join(tmp, "concat.txt")
    with open(concat_txt, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")

    silent = os.path.join(tmp, "silent.mp4")
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_txt,
            "-c",
            "copy",
            silent,
        ]
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            silent,
            "-i",
            voice_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "1",
            "-af",
            "acompressor=threshold=0.1:ratio=2:attack=6:release=160:makeup=4,"
            "loudnorm=I=-14:TP=-0.5:LRA=7",
            "-shortest",
            out_path,
        ]
    )

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[assemble] wrote {out_path}")


# ── step: captions ────────────────────────────────────────────────────────────


def step_captions(proj, base_dir):
    import captacity

    final_path = os.path.join(base_dir, "out", "final.mp4")
    ass_path = os.path.join(base_dir, "out", "captions.ass")
    captioned_path = os.path.join(base_dir, "out", "final_captioned.mp4")

    captacity.add_captions(
        final_path,
        captioned_path,
        highlight_current_word=True,
        font_size=130,
        font_color="yellow",
        stroke_width=3,
    )

    shots_json_path = os.path.join(base_dir, "shots.json")

    if os.path.exists(captioned_path) and os.path.getsize(captioned_path) > 10000:
        print("[captions] skipping — final_captioned.mp4 already exists")
        return

    if not os.path.exists(final_path):
        raise SystemExit("out/final.mp4 not found — run the 'assemble' step first")
    if not os.path.exists(shots_json_path):
        raise SystemExit("shots.json not found — run the 'shots' step first")

    timed_shots = json.load(open(shots_json_path))
    shots = proj["shots"]
    if len(timed_shots) != len(shots):
        raise SystemExit(
            f"shots.json has {len(timed_shots)} entries but project.json has {len(shots)}"
        )

    group_size = max(1, int(proj.get("caption_group_size", 3)))
    events = []

    for timed, shot in zip(timed_shots, shots):
        tokens = display_tokens(shot["phrase"])
        if not tokens:
            continue
        start = float(timed["start"])
        end = float(timed["end"])
        span = max(0.1, end - start)

        # chunk tokens into groups of `group_size`
        chunks = [tokens[k : k + group_size] for k in range(0, len(tokens), group_size)]
        n = len(chunks)
        for k, chunk in enumerate(chunks):
            s = start + span * (k / n)
            e = start + span * ((k + 1) / n)
            text = " ".join(chunk)
            text = re.sub(r"\s+", " ", text).strip().upper()
            text = text.replace("{", "(").replace("}", ")")
            events.append([s, e, text])

    # guarantee non-overlapping, contiguous events
    for k in range(len(events) - 1):
        events[k][1] = min(events[k][1], events[k + 1][0])

    with open(ass_path, "w") as f:
        f.write(ASS_HEADER)
        for s, e, text in events:
            if e > s:
                f.write(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{text}\n")

    print(f"[captions] wrote {ass_path} ({len(events)} groups)")

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            final_path,
            "-vf",
            f"ass={ass_path}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            captioned_path,
        ]
    )
    print(f"[captions] wrote {captioned_path}")


# ── main ──────────────────────────────────────────────────────────────────────

STEPS_ALL = [
    "prompts",
    "voice",
    "images",
    "watermark",
    "shots",
    "assemble",
    "captions",
]
STEPS_DEFAULT = ["voice", "images", "watermark", "shots", "assemble", "captions"]


def main():
    ap = argparse.ArgumentParser(
        description="Ink Explainer Short pipeline. AI writes project.json; this runs it."
    )
    ap.add_argument("project_json", help="path to project.json")
    ap.add_argument(
        "--steps",
        default="all",
        help=f"comma-separated steps (default: all). Available: {', '.join(STEPS_ALL)}",
    )
    ap.add_argument("--skip", default="", help="comma-separated steps to skip")
    ap.add_argument(
        "--agy",
        action="store_true",
        help="auto-generate missing shots via `npx imagine-agy` into raw/N.jpg. "
        "Without this flag, missing shots are reported and raw/prompts.md is written.",
    )
    args = ap.parse_args()

    if not os.path.exists(args.project_json):
        raise SystemExit(f"Not found: {args.project_json}")

    proj = json.load(open(args.project_json))
    base_dir = os.path.dirname(os.path.abspath(args.project_json))

    if args.steps.strip() == "all":
        steps = STEPS_DEFAULT
    else:
        steps = [s.strip() for s in args.steps.split(",")]

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    steps = [s for s in steps if s not in skip]

    for s in steps:
        if s not in STEPS_ALL:
            raise SystemExit(f"Unknown step: {s!r}. Valid: {', '.join(STEPS_ALL)}")

    print(
        f"[pipeline] {proj.get('name', 'unnamed')}  |  "
        f"image gen: {'--agy (auto)' if args.agy else 'manual / raw only'}"
    )
    print(f"[pipeline] steps: {' → '.join(steps)}")
    print(f"[pipeline] dir: {base_dir}")
    print()

    for step in steps:
        print(f"{'─' * 52}")
        if step == "prompts":
            step_prompts(proj, base_dir)
        elif step == "voice":
            step_voice(proj, base_dir)
        elif step == "images":
            step_images(proj, base_dir, use_agy=args.agy)
        elif step == "watermark":
            step_watermark(proj, base_dir)
        elif step == "shots":
            step_shots(proj, base_dir)
        elif step == "assemble":
            step_assemble(proj, base_dir)
        elif step == "captions":
            step_captions(proj, base_dir)

    print("\n[pipeline] done ✓")
    out = os.path.join(base_dir, "out", "final_captioned.mp4")
    if os.path.exists(out):
        print(f"[pipeline] deliverable: {out}")


if __name__ == "__main__":
    main()
