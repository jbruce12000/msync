"""
make_test_music.py - generate demo stereo tracks for msync.

By default it creates three top-level singles in the music/ folder.
Pass --with-albums to also generate two small demo albums (sub-folders).
"""
import argparse, os
import numpy as np, wave

sr = 44100


def fade(t, dur):
    return np.minimum(1.0, np.minimum(t / 0.5, (dur - t) / 0.5))


def tone(freq, dur, amp=0.3):
    t = np.arange(int(sr * dur)) / sr
    return amp * np.sin(2 * np.pi * freq * t) * fade(t, dur)


def write_wav(path, left, right):
    frames = np.column_stack([left, right])
    pcm = (frames * 32767).astype(np.int16)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    print("wrote", path, "(stereo, L/R differ)")


# Top-level singles ---------------------------------------------------------
singles = {
    "music/track_01_A440.wav": (tone(440, 20.0), tone(660, 20.0)),   # L: A4, R: E5
    "music/track_02_B494.wav": (tone(494, 20.0), tone(740, 20.0)),   # L: B4, R: F#5
    "music/track_03_C554.wav": (tone(554, 20.0), tone(831, 20.0)),   # L: C#5,R: G#5
}

# Demo albums (only with --with-albums) -------------------------------------
albums = {
    "music/Sunset Drive/01 Pacific Lights.wav":   (tone(523, 4.0), tone(784, 4.0)),
    "music/Sunset Drive/02 Neon Highway.wav":     (tone(587, 4.0), tone(880, 4.0)),
    "music/Sunset Drive/03 Coastal Breeze.wav":   (tone(659, 4.0), tone(988, 4.0)),
    "music/City After Rain/01 Reflections.wav":    (tone(466, 4.0), tone(698, 4.0)),
    "music/City After Rain/02 Rooftop View.wav":   (tone(494, 4.0), tone(740, 4.0)),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-albums", action="store_true",
                    help="also generate two demo albums (sub-folders)")
    args = ap.parse_args()

    for path, (left, right) in singles.items():
        write_wav(path, left, right)

    if args.with_albums:
        for path, (left, right) in albums.items():
            write_wav(path, left, right)


if __name__ == "__main__":
    main()
