"""Generates original, royalty-free background music entirely from code.

Every track is synthesized with numpy (no samples, no recordings, no external
audio files), so there is nothing to be claimed or struck. Each call produces
a different light, motivating track: randomized key, tempo, chord progression,
arpeggio pattern and -- most importantly -- a random instrument (piano,
marimba, music box, flute, guitar or organ) so consecutive videos do not all
sound the same. A soft pad sits underneath, with an octave doubling on top.

Usage:
    from audio_gen import generate_music

    generate_music(duration_seconds=58.5, out_path="music.wav", seed=123)
"""
import random
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100

# Diatonic chord progressions as scale degrees (0 = tonic). Light, uplifting
# loops that are pleasant to listen to on repeat.
PROGRESSIONS = [
    (0, 5, 3, 4),   # I - vi - IV - V  (the classic upbeat loop)
    (0, 3, 5, 4),   # I - IV - vi - V
    (0, 5, 4, 5),   # I - vi - V - vi
    (0, 3, 4, 3),   # I - IV - V - IV
    (0, 1, 4, 5),   # I - ii - V - IV (7-3-6-2 shuffle vibe)
    (0, 1, 3, 4),   # I - ii - IV - V
]

# 8-note arpeggio directions. "+" rises, "-" falls, "+-"/"-+" bounce.
ARP_PATTERNS = [
    [0, 2, 4, 7, 9, 12, 14, 16],
    [0, 2, 4, 7, 9, 7, 4, 2],
    [12, 14, 16, 19, 21, 19, 16, 14],
    [0, 4, 7, 12, 16, 19, 16, 12],
    [0, 7, 12, 16, 19, 16, 12, 7],
]

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
A4 = 440.0

INSTRUMENTS = ["piano", "marimba", "musicbox", "flute", "guitar", "organ"]


def midi_to_freq(midi: float) -> float:
    return A4 * 2.0 ** ((midi - 69) / 12.0)


class InstrumentNote:
    """A single synthesized note in one of several instrument timbres.
    All timbres are additive (harmonic stacks with an attack/decay envelope)
    so they stay 100% original and free of any recorded material."""

    def __init__(self, rng: random.Random, freq: float, dur: float, timbre: str,
                 sample_rate: int = SAMPLE_RATE):
        n = int(dur * sample_rate)
        t = np.arange(n) / sample_rate

        if timbre == "piano":
            decay = 2.6 + rng.uniform(-0.4, 0.4)
            amps = (1.0, 0.40, 0.20, 0.10, 0.05, 0.025)
            partials = 0.0
            for h, amp in enumerate(amps, start=1):
                inharm = 1.0 + 0.0003 * h * h
                partials += amp * np.sin(2 * np.pi * freq * inharm * h * t)
            body = partials + 0.10 * np.sin(2 * np.pi * freq * 1.003 * t) \
                + 0.10 * np.sin(2 * np.pi * freq * 0.997 * t)
        elif timbre == "marimba":
            decay = 7.0 + rng.uniform(-1.5, 1.5)
            body = (np.sin(2 * np.pi * freq * t)
                    + 0.45 * np.sin(2 * np.pi * freq * 4.0 * t)
                    + 0.15 * np.sin(2 * np.pi * freq * 9.0 * t))
        elif timbre == "musicbox":
            decay = 5.5 + rng.uniform(-1.0, 1.0)
            body = (np.sin(2 * np.pi * freq * t)
                    + 0.80 * np.sin(2 * np.pi * freq * 2.0 * t)
                    + 0.50 * np.sin(2 * np.pi * freq * 3.0 * t)
                    + 0.30 * np.sin(2 * np.pi * freq * 4.0 * t))
        elif timbre == "flute":
            decay = 1.2 + rng.uniform(-0.3, 0.3)
            vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.5 * t)
            body = (np.sin(2 * np.pi * freq * t * vib)
                    + 0.25 * np.sin(2 * np.pi * freq * 2.0 * t * vib))
        elif timbre == "guitar":
            decay = 4.5 + rng.uniform(-0.8, 0.8)
            body = (np.sin(2 * np.pi * freq * t)
                    + 0.50 * np.sin(2 * np.pi * freq * 2.0 * t)
                    + 0.25 * np.sin(2 * np.pi * freq * 3.0 * t)
                    + 0.10 * np.sin(2 * np.pi * freq * 4.0 * t))
        else:  # organ -- sustained, slightly detuned
            decay = 0.7 + rng.uniform(-0.2, 0.2)
            trem = 1.0 + 0.02 * np.sin(2 * np.pi * 6.0 * t)
            body = ((np.sin(2 * np.pi * freq * t)
                     + 0.50 * np.sin(2 * np.pi * freq * 2.0 * t)
                     + 0.25 * np.sin(2 * np.pi * freq * 3.0 * t)) * trem)

        body /= max(1e-9, np.abs(body).max())
        sustain = np.exp(-t * decay)
        attack = np.minimum(1.0, t / 0.012)
        self.samples = body * sustain * attack
        self.length = n


def render_music(duration: float, seed: int | None = None) -> np.ndarray:
    """Render `duration` seconds of stereo float32 audio [-1, 1].

    Randomized per call (or per seed): key, tempo, progression, pattern,
    voicing. Layered as: soft pad underneath + piano arpeggio + music-box
    octave doubling above."""
    rng = random.Random(seed)
    sr = SAMPLE_RATE

    total = int(duration * sr)
    mix = np.zeros((2, total), dtype=np.float64)

    instrument = rng.choice(INSTRUMENTS)
    key_root = rng.choice([0, 2, 4, 5, 7, 9])
    # Major scale intervals (diatonic degrees for the progression).
    scale = [0, 2, 4, 5, 7, 9, 11]
    progression = rng.choice(PROGRESSIONS)
    arp = rng.choice(ARP_PATTERNS)
    bpm = rng.randint(74, 96)
    beat = 60.0 / bpm
    chord_secs = beat * 4.0          # one chord per bar
    note_secs = chord_secs / 8.0     # eighth-note arpeggio
    low_octave = rng.randint(3, 4)   # bass register
    mid_octave = 5
    high_octave = 6                  # music box doubling
    velocity = rng.uniform(0.55, 0.75)
    pad_vol = rng.uniform(0.12, 0.18)   # warm bed, not a distant drone

    n_bars = int(np.ceil(total / sr / chord_secs)) + 1

    bar = 0
    bar_start = 0.0
    while bar_start < duration and bar < n_bars:
        degree = progression[bar % len(progression)]
        # Chord: triad on the degree (+1 octave-up fifth for brightness).
        root_semi = scale[degree]
        chord = [root_semi, root_semi + 4, root_semi + 7, root_semi + 12]

        # --- soft pad (detuned sines, slow attack) ---
        pad_start = int(bar_start * sr)
        pad_len = min(int(chord_secs * sr), total - pad_start)
        if pad_len > 0:
            pt = np.arange(pad_len) / sr
            pad = np.zeros(pad_len)
            for semi in chord:
                f = midi_to_freq(low_octave * 12 + semi)
                pad += np.sin(2 * np.pi * f * pt)
                pad += 0.5 * np.sin(2 * np.pi * f * 1.002 * pt)
                pad += 0.5 * np.sin(2 * np.pi * f * 0.998 * pt)
            pad /= max(1e-9, np.abs(pad).max())
            fade = np.minimum(1.0, pt / 1.2) * np.minimum(1.0, (chord_secs - pt) / 1.2)
            pad *= fade
            mix[:, pad_start:pad_start + pad_len] += pad_vol * pad

        # --- arpeggio notes ---
        for i, semi_off in enumerate(arp):
            t0 = bar_start + i * note_secs
            if t0 >= duration:
                break
            midi = low_octave * 12 + root_semi + semi_off
            note_dur = note_secs * 1.6
            note = InstrumentNote(rng, midi_to_freq(midi), note_dur, instrument)
            start = int(t0 * sr)
            end = min(start + note.length, total)
            vel = velocity * rng.uniform(0.8, 1.15)
            lr = 0.55 + rng.uniform(-0.25, 0.25)  # gentle stereo pan
            mix[0, start:end] += vel * (2 - lr) * note.samples[: end - start]
            mix[1, start:end] += vel * lr * note.samples[: end - start]

            # Octave doubling above on bar-start beats -- only for the
            # brighter instruments, where it belongs.
            if i % 8 == 0 and instrument in ("piano", "marimba", "musicbox"):
                m_midi = midi + 12
                mnote = InstrumentNote(rng, midi_to_freq(m_midi), note_dur * 0.9, instrument)
                mend = min(start + mnote.length, total)
                mvel = vel * 0.22
                mix[0, start:mend] += mvel * (2 - lr) * mnote.samples[: mend - start]
                mix[1, start:mend] += mvel * lr * mnote.samples[: mend - start]

        bar += 1
        bar_start += chord_secs

    # Master fade in/out.
    n_fade = int(1.2 * sr)
    fade_in = np.minimum(1.0, np.arange(total) / n_fade)
    fade_out = np.minimum(1.0, (total - np.arange(total)) / n_fade)
    mix *= fade_in * fade_out

    # Gentle one-pole low-pass (cutoff ~8 kHz) to tame harsh digital partials.
    b = 0.30
    y = np.empty_like(mix)
    for ch in range(2):
        acc = 0.0
        for i in range(total):
            acc = b * acc + (1.0 - b) * mix[ch, i]
            y[ch, i] = acc
    mix = y

    # Soft-clip (tanh drive) to thicken loudness without distortion, then
    # normalize to a full, clearly-audible peak.
    drive = 2.0
    mix = np.tanh(mix * drive) / np.tanh(drive)
    peak = max(1e-9, float(np.abs(mix).max()))
    target_peak = 0.89
    mix *= target_peak / peak

    return mix.astype(np.float32)


def write_wav(samples: np.ndarray, out_path, sample_rate: int = SAMPLE_RATE):
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    interleaved = np.zeros(pcm.shape[1] * 2, dtype="<i2")
    interleaved[0::2] = pcm[0, :]
    interleaved[1::2] = pcm[1, :]
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(interleaved.tobytes())


def generate_music(duration: float, out_path: Path, seed: int | None = None) -> Path:
    """Render a random original track and write it as a 16-bit stereo WAV."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    samples = render_music(duration, seed=seed)
    write_wav(samples, out_path)
    return out_path


if __name__ == "__main__":
    import sys
    import tempfile
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    out = Path(tempfile.gettempdir()) / "audio_gen_test.wav"
    generate_music(dur, out)
    info = np.fromfile(out, dtype=np.uint8)
    print(f"wrote {out} ({info.size / 1024:.0f} KB, {dur:.0f}s)")
