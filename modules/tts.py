#!/usr/bin/env python3
"""
NeMo TTS Module - Texto → Audio en español

Modelo: tts_es_fastpitch_multispeaker + tts_es_hifigan_ft_fastpitch_multispeaker
  - 174 voces latinas (Argentina, Chile, Colombia, Perú, Puerto Rico, Venezuela)
  - Sample rate: 44.1kHz
  - RTF: 0.01-0.13x (muy rápido)
"""

import argparse
import sys
import time
import numpy as np
import soundfile as sf


class TTSNemo:
    """NeMo FastPitch + HiFiGAN para español."""

    def __init__(self, speaker_id=0):
        self.speaker_id = speaker_id
        self.spec_gen = None
        self.vocoder = None

    def load(self):
        import torch
        from nemo.collections.tts.models import FastPitchModel, HifiGanModel

        print(f"[TTS] Cargando NeMo FastPitch+HiFiGAN (speaker={self.speaker_id})...")
        t0 = time.time()

        self.spec_gen = FastPitchModel.from_pretrained('tts_es_fastpitch_multispeaker')
        self.vocoder = HifiGanModel.from_pretrained('tts_es_hifigan_ft_fastpitch_multispeaker')
        self.spec_gen.eval()
        self.vocoder.eval()

        print(f"[TTS] Modelo cargado en {time.time() - t0:.1f}s")

    def synthesize(self, text):
        import torch

        print(f"[TTS] Generando: speaker={self.speaker_id}")
        t0 = time.time()

        parsed = self.spec_gen.parse(text)
        speaker = torch.tensor([self.speaker_id]).long().to(self.spec_gen.device)
        spectrogram = self.spec_gen.generate_spectrogram(tokens=parsed, speaker=speaker)
        audio = self.vocoder.convert_spectrogram_to_audio(spec=spectrogram)
        audio_np = audio.squeeze().detach().cpu().numpy()

        elapsed = time.time() - t0
        duration = len(audio_np) / 44100
        print(f"[TTS] Síntesis: {elapsed:.2f}s → {duration:.1f}s audio (RTF={elapsed/duration:.2f}x)")

        return audio_np, 44100


def load_model(speaker_id=0):
    """Carga el modelo TTS."""
    tts = TTSNemo(speaker_id)
    tts.load()
    return tts


def synthesize(model, text, output_path):
    """Sintetiza texto y guarda como WAV."""
    audio, sr = model.synthesize(text)
    sf.write(output_path, audio, sr)
    print(f"[TTS] Guardado: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="NeMo TTS - Texto a audio en español")
    parser.add_argument("text", help="Texto a sintetizar")
    parser.add_argument("-o", "--output", default="output/test_tts.wav", help="Archivo WAV de salida")
    parser.add_argument("-s", "--speaker", type=int, default=0, help="Speaker ID (0-173)")
    args = parser.parse_args()

    tts = load_model(args.speaker)
    synthesize(tts, args.text, args.output)


if __name__ == "__main__":
    main()
