import os
import imageio_ffmpeg
import yt_dlp

SOUNDS = {
    "vine_boom": "ytsearch1:vine boom sound effect",
    "airhorn": "ytsearch1:mlg airhorn sound effect",
    "bruh": "ytsearch1:bruh sound effect 2",
    "metal_pipe": "ytsearch1:metal pipe falling sound effect",
    "sad_violin": "ytsearch1:sad violin sound effect",
    "drumroll": "ytsearch1:drum roll sound effect",
    "victory": "ytsearch1:final fantasy victory fanfare sound effect",
    "sheesh": "ytsearch1:sheesh sound effect",
}

def main():
    sounds_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
    os.makedirs(sounds_dir, exist_ok=True)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    print("Checking and downloading soundboard audio files...")
    for name, query in SOUNDS.items():
        target_path = os.path.join(sounds_dir, f"{name}.mp3")
        if os.path.exists(target_path):
            print(f"✓ Already present: {name}")
            continue

        print(f"Downloading {name}...")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(sounds_dir, f"{name}.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
            "ffmpeg_location": ffmpeg_exe,
            "quiet": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([query])
            print(f"✓ Downloaded {name}")
        except Exception as e:
            print(f"✗ Failed to download {name}: {e}")

    print("\nSoundboard setup complete!")

if __name__ == "__main__":
    main()
