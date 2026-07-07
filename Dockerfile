FROM nvcr.io/nvidia/pytorch:25.11-py3

RUN apt-get update && apt-get install -y \
    libsndfile1 ffmpeg libopus-dev pkg-config -qq \
    && rm -rf /var/lib/apt/lists/*

# torchaudio desde pytorch.org cu130 (NGC no lo incluye)
RUN pip install --no-cache-dir \
    torchaudio torchcodec --index-url https://download.pytorch.org/whl/cu130

# NeMo toolkit con ASR y TTS + openai client para vLLM + security deps
RUN pip install --no-cache-dir \
    "nemo_toolkit[asr,tts]" \
    soundfile \
    scipy \
    openai \
    fastapi \
    "uvicorn[standard]" \
    "websockets<14" \
    silero-vad \
    num2words \
    python-multipart \
    bcrypt \
    slowapi

# vLLM (single-python; coexiste con NeMo 2.7.3 + transformers 4.57 — validado en x86 y arm64).
# Sirve el LLM en :8002. vLLM 0.23 maneja Qwen FP8 en Hopper nativo (sin hot-patch).
RUN pip install --no-cache-dir vllm

WORKDIR /app
