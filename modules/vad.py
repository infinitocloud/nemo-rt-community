#!/usr/bin/env python3
"""
Streaming Voice Activity Detection using Silero VAD.

Per-connection state machine that detects speech boundaries
from continuous 16kHz mono PCM audio chunks.
"""

import collections

import numpy as np
import torch


class StreamingVAD:
    """Per-connection streaming VAD state machine.

    States: IDLE -> SPEAKING -> (silence detected) -> triggers pipeline -> IDLE

    Usage:
        model, _ = torch.hub.load('snakers4/silero-vad', 'silero_vad')
        vad = StreamingVAD(model)
        for chunk in audio_stream:
            result = vad.process_chunk(chunk)
            if result and result["event"] == "speech_end":
                run_pipeline(result["audio"])
    """

    def __init__(
        self,
        model,
        threshold: float = 0.35,
        sampling_rate: int = 16000,
        silence_threshold_ms: int = 600,
        min_speech_ms: int = 150,
        speech_pad_ms: int = 300,
        chunk_size: int = 512,
    ):
        self.model = model
        self.threshold = threshold
        self.sr = sampling_rate
        self.chunk_size = chunk_size

        self.silence_threshold_samples = int(sampling_rate * silence_threshold_ms / 1000)
        self.min_speech_samples = int(sampling_rate * min_speech_ms / 1000)

        # Pre-buffer: keep last N chunks to capture speech onset
        pre_buffer_chunks = max(1, int(sampling_rate * speech_pad_ms / 1000) // chunk_size + 1)
        self._pre_buffer = collections.deque(maxlen=pre_buffer_chunks)

        # Reset model RNN state on init
        self.model.reset_states()

        # State
        self.state = "IDLE"
        self.audio_buffer: list[np.ndarray] = []
        self.silence_counter = 0
        self.total_speech_samples = 0

    def reset(self):
        """Reset state for new utterance detection."""
        self.model.reset_states()
        self.state = "IDLE"
        self.audio_buffer = []
        self._pre_buffer.clear()
        self.silence_counter = 0
        self.total_speech_samples = 0

    def process_chunk(self, pcm_int16_bytes: bytes) -> dict | None:
        """Process a chunk of 16kHz mono int16 PCM.

        Args:
            pcm_int16_bytes: Raw bytes of int16 PCM audio (1024 bytes = 512 samples).

        Returns:
            dict with event info, or None if nothing notable:
            - {"event": "speech_start"} when speech begins
            - {"event": "speech_end", "audio": np.ndarray} when silence after speech
            - {"event": "speech_too_short"} when speech was too brief (noise)
        """
        samples = np.frombuffer(pcm_int16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        chunk_tensor = torch.from_numpy(samples)

        speech_prob = self.model(chunk_tensor, self.sr).item()

        if self.state == "IDLE":
            self._pre_buffer.append(samples)

            if speech_prob >= self.threshold:
                self.state = "SPEAKING"
                # Include pre-buffer to capture speech onset
                self.audio_buffer = list(self._pre_buffer)
                self.total_speech_samples = len(samples)
                self.silence_counter = 0
                return {"event": "speech_start"}

        elif self.state == "SPEAKING":
            self.audio_buffer.append(samples)

            if speech_prob >= self.threshold:
                self.total_speech_samples += len(samples)
                self.silence_counter = 0
            else:
                self.silence_counter += len(samples)

                if self.silence_counter >= self.silence_threshold_samples:
                    if self.total_speech_samples >= self.min_speech_samples:
                        audio = np.concatenate(self.audio_buffer)
                        self._reset_for_next()
                        return {"event": "speech_end", "audio": audio}
                    else:
                        self._reset_for_next()
                        return {"event": "speech_too_short"}

        return None

    def _reset_for_next(self):
        """Reset state after speech end, ready for next utterance."""
        self.state = "IDLE"
        self.audio_buffer = []
        self._pre_buffer.clear()
        self.silence_counter = 0
        self.total_speech_samples = 0
        self.model.reset_states()
