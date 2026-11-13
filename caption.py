import os
import json
from faster_whisper import WhisperModel

def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"

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

# Load config to get caption group size
proj = json.load(open("project.json"))
group_size = proj.get("caption_group_size", 3)

print("Loading Whisper model (base) on CPU...")
# 'base' is fast and accurate enough for clear TTS
model = WhisperModel("base", device="cpu", compute_type="int8")

print("Transcribing voice.wav...")
segments, _ = model.transcribe("voice/voice.wav", word_timestamps=True)

events = []
current_group = []
group_start = 0.0

for segment in segments:
    for word in segment.words:
        if not current_group:
            group_start = word.start
        
        current_group.append(word)
        
        if len(current_group) >= group_size:
            # We have a full group, write it
            text = " ".join([w.word.strip() for w in current_group]).upper()
            events.append([group_start, word.end, text])
            current_group = []

# Catch any leftover words
if current_group:
    text = " ".join([w.word.strip() for w in current_group]).upper()
    events.append([group_start, current_group[-1].end, text])

# Write ASS file
os.makedirs("out", exist_ok=True)
with open("out/captions.ass", "w") as f:
    f.write(ASS_HEADER)
    for s, e, text in events:
        f.write(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{text}\n")

print(f"Wrote out/captions.ass ({len(events)} groups)")
