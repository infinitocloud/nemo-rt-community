#!/usr/bin/env python3
"""
NeMo STT Module - Audio → Texto en español

Modelos disponibles:
  - stt_es_fastconformer_hybrid_large_pc  (recomendado, más rápido)
  - stt_es_conformer_ctc_large
  - stt_es_conformer_transducer_large
  - stt_enes_conformer_ctc_large  (bilingüe inglés-español)
  - stt_multilingual_fastconformer_hybrid_large_pc  (multilingüe)
"""

import argparse
import sys
import time


class STTNemo:
    """NeMo ASR para español."""

    # Usar CTC para compatibilidad con forward directo (bypass Lhotse bug)
    DEFAULT_MODEL = "stt_es_conformer_ctc_large"

    def __init__(self, model_name=None):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.model = None

    def load(self):
        from nemo.collections.asr.models import ASRModel

        print(f"[STT] Cargando NeMo {self.model_name}...")
        t0 = time.time()

        self.model = ASRModel.from_pretrained(self.model_name)
        self.model.eval()

        print(f"[STT] Modelo cargado en {time.time() - t0:.1f}s")

    def transcribe(self, audio_path):
        import torch
        import soundfile as sf

        print(f"[STT] Transcribiendo: {audio_path}")
        t0 = time.time()

        # Cargar audio
        audio, sr = sf.read(audio_path)

        # Resamplear a 16kHz si es necesario
        if sr != 16000:
            import torchaudio
            audio = torchaudio.functional.resample(
                torch.tensor(audio).unsqueeze(0), sr, 16000
            ).squeeze().numpy()

        # Forward directo (bypass Lhotse bug en NeMo 2.6.1)
        with torch.no_grad():
            audio_tensor = torch.tensor(audio).float().unsqueeze(0).to(self.model.device)
            audio_len = torch.tensor([audio_tensor.shape[1]]).to(self.model.device)

            log_probs, encoded_len, _ = self.model.forward(
                input_signal=audio_tensor,
                input_signal_length=audio_len
            )

            # Decodificar CTC
            hypotheses = self.model.decoding.ctc_decoder_predictions_tensor(
                log_probs, encoded_len
            )
            text = hypotheses[0].text if hasattr(hypotheses[0], 'text') else str(hypotheses[0])

        elapsed = time.time() - t0
        print(f"[STT] Tiempo: {elapsed:.2f}s")
        print(f"[STT] Texto: {text}")

        return text


def load_model(model_name=None):
    """Carga el modelo STT."""
    stt = STTNemo(model_name)
    stt.load()
    return stt


def transcribe(model, audio_path):
    """Transcribe un archivo de audio."""
    return model.transcribe(audio_path)


def main():
    parser = argparse.ArgumentParser(description="NeMo STT - Audio a texto en español")
    parser.add_argument("audio", help="Archivo de audio WAV")
    parser.add_argument("-m", "--model", default=None,
                        help="Modelo NeMo ASR (default: stt_es_fastconformer_hybrid_large_pc)")
    args = parser.parse_args()

    stt = load_model(args.model)
    text = transcribe(stt, args.audio)

    if not text:
        sys.exit(1)

    return text


if __name__ == "__main__":
    main()
