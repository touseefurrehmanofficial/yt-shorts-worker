"""Generate 10 original background-music tracks in the genres US creators most
often use for videos that have no dialog: lofi, upbeat pop, motivational EDM,
cinematic, acoustic guitar, ukulele, hip-hop beat, emotional piano, ambient
chill and funky. Everything is synthesized from code (numpy) -- no samples,
no recordings, nothing to claim. Reuses audio_gen.py's note/pad synthesis.

Output: MP3 files written to a folder given as argv[1].
"""
import random
import subprocess
import sys
from pathlib import Path

import numpy as np

import audio_gen as AG

SAMPLE_RATE = AG.SAMPLE_RATE

# genre -> (bpm, instrument, progression (scale degrees), drum pattern,
#           bass, extra flavor)
GENRES = {
    "lofi":            (78, "piano",   (0, 5, 3, 4), [("crackle",)], False),
    "upbeat_pop":      (108, "guitar", (0, 3, 5, 4), [("kick", 0.0, 0.9), ("hat", 0.5, 0.25),
                                                      ("kick", 0.5, 0.6), ("hat", 0.0, 0.2),
                                                      ("hat", 0.25, 0.2), ("hat", 0.75, 0.2)], True),
    "motivational_edm": (124, "organ", (0, 5, 3, 4), [("kick", 0.0, 0.95), ("kick", 0.25, 0.95),
                                                      ("kick", 0.5, 0.95), ("kick", 0.75, 0.95),
                                                      ("hat", 0.125, 0.18), ("hat", 0.375, 0.18),
                                                      ("hat", 0.625, 0.18), ("hat", 0.875, 0.18)], True),
    "cinematic":       (84, "organ",  (0, 3, 5, 4), [], True),
    "acoustic_guitar": (96, "guitar", (0, 5, 4, 5), [], False),
    "ukulele":         (100, "guitar", (0, 3, 5, 4), [], False),
    "hiphop":          (88, "piano",  (0, 5, 2, 4), [("kick", 0.0, 0.9), ("snare", 0.25, 0.5),
                                                    ("kick", 0.375, 0.7), ("snare", 0.5, 0.5),
                                                    ("kick", 0.75, 0.9), ("hat", 0.625, 0.15)], True),
    "emotional_piano": (72, "piano",  (0, 5, 3, 4), [], False),
    "ambient_chill":   (70, "flute",  (0, 5, 4, 5), [], False),
    "funky":           (104, "guitar", (0, 3, 5, 4), [("kick", 0.0, 0.9), ("snare", 0.25, 0.5),
                                                     ("kick", 0.5, 0.8), ("snare", 0.75, 0.5),
                                                     ("hat", 0.125, 0.2), ("hat", 0.375, 0.2),
                                                     ("hat", 0.625, 0.2), ("hat", 0.875, 0.2)], True),
}

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_KEYS = {"hiphop", "emotional_piano", "cinematic", "lofi"}


def synth_kick(rng, sr, secs=0.35):
    n = int(secs * sr)
    t = np.arange(n) / sr
    f = 52.0 * np.exp(-t * 11.0) + 34.0
    phase = 2 * np.pi * np.cumsum(f) / sr
    return np.sin(phase) * np.exp(-t * 6.5)


def synth_snare(nrng, sr, secs=0.22):
    n = int(secs * sr)
    t = np.arange(n) / sr
    noise = nrng.normal(0, 1, n)
    return (noise * np.exp(-t * 28.0) + 0.4 * np.sin(2 * np.pi * 190 * t)
            * np.exp(-t * 20.0)) / 1.4


def synth_hat(nrng, sr, secs=0.06):
    n = int(secs * sr)
    t = np.arange(n) / sr
    noise = nrng.normal(0, 1, n)
    return noise * np.exp(-t * 130.0)


def render_genre(genre: str, duration: float, seed: int) -> np.ndarray:
    bpm, instrument, progression, drums, use_bass = GENRES[genre]
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    sr = SAMPLE_RATE
    total = int(duration * sr)
    mix = np.zeros((2, total), dtype=np.float64)

    scale = MINOR_KEYS and (MAJOR_SCALE if genre not in MINOR_KEYS else [0, 2, 3, 5, 7, 8, 10])
    key_root = rng.choice([0, 2, 4, 5, 7, 9])
    arp = rng.choice(AG.ARP_PATTERNS)
    beat = 60.0 / bpm
    chord_secs = beat * 4.0
    note_secs = chord_secs / 8.0
    low_octave = rng.randint(3, 4)
    velocity = rng.uniform(0.5, 0.7)
    pad_vol = rng.uniform(0.12, 0.16)

    n_bars = int(np.ceil(total / sr / chord_secs)) + 1
    bar = 0
    bar_start = 0.0
    while bar_start < duration and bar < n_bars:
        degree = progression[bar % len(progression)]
        root_semi = scale[degree]
        chord = [root_semi, root_semi + 4, root_semi + 7, root_semi + 12]

        # pad
        pad_start = int(bar_start * sr)
        pad_len = min(int(chord_secs * sr), total - pad_start)
        if pad_len > 0:
            pt = np.arange(pad_len) / sr
            pad = np.zeros(pad_len)
            for semi in chord:
                f = AG.midi_to_freq(low_octave * 12 + semi)
                pad += np.sin(2 * np.pi * f * pt)
                pad += 0.5 * np.sin(2 * np.pi * f * 1.002 * pt)
                pad += 0.5 * np.sin(2 * np.pi * f * 0.998 * pt)
            pad /= max(1e-9, np.abs(pad).max())
            fade = np.minimum(1.0, pt / 1.2) * np.minimum(1.0, (chord_secs - pt) / 1.2)
            pad *= fade
            if genre == "cinematic":
                pad_vol_now = 0.30
            elif genre == "ambient_chill":
                pad_vol_now = 0.25
            else:
                pad_vol_now = pad_vol
            mix[:, pad_start:pad_start + pad_len] += pad_vol_now * pad

        # arpeggio
        for i, semi_off in enumerate(arp):
            t0 = bar_start + i * note_secs
            if t0 >= duration:
                break
            midi = low_octave * 12 + root_semi + semi_off
            if genre == "ukulele":
                midi += 12
            note_dur = note_secs * 1.6
            if genre in ("emotional_piano", "lofi"):
                note_dur *= 2.0
            note = AG.InstrumentNote(rng, AG.midi_to_freq(midi), note_dur, instrument)
            start = int(t0 * sr)
            end = min(start + note.length, total)
            vel = velocity * rng.uniform(0.8, 1.15)
            lr = 0.55 + rng.uniform(-0.25, 0.25)
            mix[0, start:end] += vel * (2 - lr) * note.samples[: end - start]
            mix[1, start:end] += vel * lr * note.samples[: end - start]

        # drums + bass per pattern step (frac of the bar)
        for step in drums:
            frac = step[1] if len(step) == 3 else 0.0
            gain = step[2] if len(step) == 3 else 1.0
            t0 = bar_start + frac * chord_secs
            start = int(t0 * sr)
            if start >= total:
                continue
            if step[0] == "kick":
                s = synth_kick(rng, sr)
            elif step[0] == "snare":
                s = synth_snare(nrng, sr)
            elif step[0] == "hat":
                s = synth_hat(nrng, sr)
            else:  # crackle flavor
                n = int(0.02 * sr)
                s = nrng.normal(0, 1, n) * 0.05
            end = min(start + len(s), total)
            mix[0, start:end] += gain * s[: end - start]
            mix[1, start:end] += gain * s[: end - start]

        if use_bass:
            bass_note = AG.InstrumentNote(
                rng, AG.midi_to_freq((low_octave - 1) * 12 + root_semi), chord_secs * 0.9, "organ")
            bstart = pad_start
            bend = min(bstart + bass_note.length, total)
            mix[:, bstart:bend] += 0.55 * bass_note.samples[: bend - bstart]

        bar += 1
        bar_start += chord_secs

    # master chain (same as audio_gen): fade, low-pass, soft clip, normalize
    n_fade = int(1.2 * sr)
    fade_in = np.minimum(1.0, np.arange(total) / n_fade)
    fade_out = np.minimum(1.0, (total - np.arange(total)) / n_fade)
    mix *= fade_in * fade_out
    b = 0.30
    y = np.empty_like(mix)
    for ch in range(2):
        acc = 0.0
        for i in range(total):
            acc = b * acc + (1.0 - b) * mix[ch, i]
            y[ch, i] = acc
    mix = y
    # Compression + loudness normalization: every genre lands at the same
    # mean level (-22 dBFS), so no track jumps out as louder/quieter.
    drive = 3.5
    mix = np.tanh(mix * drive) / np.tanh(drive)
    rms = max(1e-9, float(np.sqrt(np.mean(mix ** 2))))
    mix *= 10.0 ** (-22.0 / 20.0) / rms
    return mix.astype(np.float32)


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = list(GENRES)
    for i, genre in enumerate(names, 1):
        seed = 9000 + i
        samples = render_genre(genre, 60.0, seed)
        wav = out_dir / f"{genre}.wav"
        AG.write_wav(samples, wav)
        mp3 = out_dir / f"bg_track_{i:02d}_{genre}.mp3"
        subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-af", "volume=1.0",
                        "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)],
                       capture_output=True)
        wav.unlink(missing_ok=True)
        print(f"{i:02d} {genre}: {mp3.name}")


if __name__ == "__main__":
    main()
