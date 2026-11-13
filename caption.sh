uv run caption.py

ffmpeg -y -i out/final.mp4 -vf "ass=out/captions.ass" -c:v libx264 -preset fast -crf 18 -c:a copy out/final_captioned.mp4
