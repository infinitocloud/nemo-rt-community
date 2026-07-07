#!/usr/bin/env python3
"""
Web UI for Speech-to-Speech Pipeline

FastAPI + WebSocket server.
Microphone capture in browser → STT → LLM streaming → TTS per sentence → audio playback.
TTS engine: NeMo FastPitch (local GPU, 174 voices).
"""

import asyncio
import base64
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import num2words
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from modules.config_store import ConfigStore

# ─── Rate limiting (slowapi) ─────────────────────────────────────────────────
# Keys read from env vars so ops can tune without code changes.
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

_RL_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").strip().lower() in ("1", "true", "yes")

RL_DEFAULT    = os.environ.get("RATE_LIMIT_DEFAULT",    "60/minute")
RL_AUTH       = os.environ.get("RATE_LIMIT_AUTH",       "10/minute")
RL_TENANTS    = os.environ.get("RATE_LIMIT_TENANTS",    "30/minute")
RL_WS_CONNECT = os.environ.get("RATE_LIMIT_WS_CONNECT", "30/minute")


def _client_ip(request) -> str:
    """Get client IP, trusting X-Forwarded-For from our own nginx reverse proxy.
    Falls back to direct remote_addr if header is missing.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Take leftmost (original client) IP
        return xff.split(",")[0].strip()
    return get_remote_address(request)


# Limiter instance — disabled returns a no-op limiter (decorators stay valid)
limiter = Limiter(
    key_func=_client_ip,
    default_limits=[RL_DEFAULT] if _RL_ENABLED else [],
    enabled=_RL_ENABLED,
)

app = FastAPI(title="S2S NVIDIA")
app.state.limiter = limiter


async def _rate_limit_handler(request, exc: RateLimitExceeded):
    """Custom 429 with Retry-After header."""
    response = JSONResponse(
        {"error": "rate limit exceeded", "detail": str(exc.detail)},
        status_code=429,
    )
    # slowapi puts retry_after in seconds on the exception
    retry_after = getattr(exc, "retry_after", None) or 60
    response.headers["Retry-After"] = str(retry_after)
    return response


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def block_key_in_query(request: Request, call_next):
    """Reject any HTTP request that carries the API key in the URL query string.
    Sessions must be established via POST /api/auth/login → HttpOnly cookie."""
    if "key" in request.query_params:
        return JSONResponse(
            {
                "error": "api_key_in_url_not_allowed",
                "detail": "API key in URL is unsafe (leaks in logs). POST /api/auth/login with {key} in the JSON body to set a session cookie.",
            },
            status_code=400,
        )
    return await call_next(request)


# Resolve paths against the source file, not the CWD — the process is often
# launched with a different working directory (start.sh, systemd, supervisord)
# and a relative "static" would error with "Directory does not exist".
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(_APP_DIR, "static")), name="static")

# --- Config store (file-based, database-free) ---
db: ConfigStore | None = None

# --- RAG ---
rag_queue = None  # RAG is a Pro feature (not in Community)

# --- Security config ---
API_KEY = os.environ.get("API_KEY", "").strip()
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "20"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
_active_connections = 0
_connections_lock = threading.Lock()

# Fail-loud: refuse to start in production without an API key.
# Set ENV=dev (or unset) to allow empty-key dev mode.
_ENV = os.environ.get("ENV", "dev").strip().lower()
if not API_KEY and _ENV in ("prod", "production"):
    raise RuntimeError(
        "API_KEY is empty but ENV=prod. Refusing to start without auth. "
        "Set a strong API_KEY in your environment, or use ENV=dev to allow "
        "anonymous access (development only)."
    )
if not API_KEY:
    print("[SECURITY] ⚠️  API_KEY is empty — anonymous access is allowed (dev mode). "
          "Set ENV=prod for stricter production gating.")

# --- Cookie-based session auth ---
# After POST /api/auth/login the client receives an HttpOnly+Secure+SameSite=Strict
# cookie holding an opaque server-side session token. The raw API key is never
# stored in JS / localStorage / URL.
import secrets
SESSION_COOKIE = "nemo_session"
SESSION_TTL    = int(os.environ.get("SESSION_TTL", str(8 * 3600)))  # seconds
# Secure=True requires HTTPS. In dev (ENV=dev) we accept http://localhost so the
# browser will still send the cookie.
COOKIE_SECURE  = _ENV in ("prod", "production")
_sessions: dict[str, dict] = {}  # token -> {api_key, expires_at}

def _new_session_token(api_key: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"api_key": api_key, "expires_at": time.time() + SESSION_TTL}
    return token

def _resolve_session(token: str | None) -> str | None:
    """Return the api_key bound to a session token, or None if missing/expired."""
    if not token:
        return None
    s = _sessions.get(token)
    if not s:
        return None
    if s["expires_at"] < time.time():
        _sessions.pop(token, None)
        return None
    return s["api_key"]

def _revoke_session(token: str | None):
    if token:
        _sessions.pop(token, None)

def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def _check_api_key(key: str | None) -> bool:
    """Validate API key. Returns True if auth is disabled or key matches admin key."""
    if not API_KEY:
        return True
    return key == API_KEY


async def _check_any_key(key: str | None) -> bool:
    """Validate the API key. Returns True if auth is disabled or key matches."""
    if not API_KEY:
        return True
    return key == API_KEY


def _check_origin(origin: str | None) -> bool:
    """Validate request origin. Returns True if no restrictions or origin matches.
    Allows connections without Origin header (CLI, scripts, tools)."""
    if not ALLOWED_ORIGINS:
        return True
    if not origin:
        return True  # No origin = not a browser (CLI/scripts) — auth via API key
    return origin in ALLOWED_ORIGINS


# --- Global state ---
models_ready = False
tts_ready = False
loading_status = {"vad": "pending", "stt": "pending", "llm": "pending", "tts": "pending", "warmup": "pending"}

# Executors DEDICADOS de 1 thread para STT y TTS.
# cuDNN crea su handle de forma lazy POR THREAD: la primera inferencia en un
# thread nuevo paga ~4s de init. Si STT/TTS corren SIEMPRE en el mismo thread
# (y lo precalentamos ahi en el arranque), la 1ra transcripcion real ya sale
# rapida en vez de los 4s de cold start. (El executor default de asyncio rota
# entre varios threads → cada uno pagaba el init en las primeras requests.)
_stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
_tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

# Models
vad_model = None
vad_model_buffer = None  # JIT bytes for per-connection clones
stt_model = None
stt_queue = None  # STTBatchQueue (initialized on startup)
llm_client = None
spec_gen = None
vocoder = None
spec_gen_en = None
vocoder_en = None
tts_queue = None
_speaker_cache = {}
_speaker_cache_en = {}

SPEAKER_ID = int(os.environ.get("SPEAKER_ID", "50"))
# LLM endpoint — vLLM is the canonical engine. TRTLLM_URL kept as deprecated alias
# for backwards-compat with old .env files; will be removed in a future release.
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    os.environ.get("TRTLLM_URL", "http://vllm:8000/v1"),
)
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen3-8B-FP8")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "128"))
LLM_TOP_P = float(os.environ.get("LLM_TOP_P", "0.9"))
# Default 0.0 — safe en Blackwell. Subir solo via env si vLLM ya parchó el
# bug de apply_penalties → scatter_add_ que crashea como "CUDA unknown error"
# (ver .env.example para contexto).
LLM_FREQ_PENALTY = float(os.environ.get("LLM_FREQ_PENALTY", "0.0"))
SYSTEM_PROMPT = "You are a concise, friendly voice assistant. Always reply in the EXACT language the user just spoke: English in -> English out, Spanish in -> Spanish out. Match their language every time; never default to one language. Keep answers natural and brief."
SETTINGS_FILE = "settings.json"


async def load_settings() -> dict:
    """Load settings from the config store."""
    if db is None:
        return {}
    entries = await db.get_all_config()
    result = {}
    for e in entries:
        # Try to parse JSON values (numbers, booleans), keep strings as-is
        try:
            result[e.key] = json.loads(e.value)
        except (json.JSONDecodeError, ValueError):
            result[e.key] = e.value
    return result


# --- Model loading ---

def load_models():
    """Load VAD + STT + LLM + TTS models with GPU warmup."""
    global vad_model, vad_model_buffer, stt_model, llm_client, spec_gen, vocoder, spec_gen_en, vocoder_en, models_ready, tts_ready

    from nemo.collections.asr.models import ASRModel
    from nemo.collections.tts.models import FastPitchModel, HifiGanModel
    from openai import AsyncOpenAI

    t0 = time.time()
    print("[INIT] Loading models...")

    # STT latencia consistente: evitar el autotuning de cuDNN por-forma de input
    # (con benchmark=True, cada nuevo largo de audio recompila → primeras transcripciones 4-8s).
    torch.backends.cudnn.benchmark = False

    # VAD (tiny, <1s)
    loading_status["vad"] = "loading"
    print("[VAD] Loading Silero VAD...")
    vad_model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        trust_repo=True,
    )
    vad_model.eval()
    # Save JIT model to buffer for per-connection cloning
    # (Silero VAD is RNN-stateful; shared model corrupts state across connections)
    vad_model_buffer = io.BytesIO()
    torch.jit.save(vad_model, vad_model_buffer)
    loading_status["vad"] = "done"
    print("[VAD] Ready")

    # STT
    loading_status["stt"] = "loading"
    print("[STT] Loading Conformer CTC (bilingual EN-ES)...")
    stt_model = ASRModel.from_pretrained("stt_enes_conformer_ctc_large")
    stt_model.eval()
    loading_status["stt"] = "done"

    # TTS ES (load before LLM so voice preview is available sooner)
    loading_status["tts"] = "loading"
    print(f"[TTS] Loading FastPitch+HiFiGAN ES (speaker={SPEAKER_ID})...")
    spec_gen = FastPitchModel.from_pretrained("tts_es_fastpitch_multispeaker")
    vocoder = HifiGanModel.from_pretrained("tts_es_hifigan_ft_fastpitch_multispeaker")
    spec_gen.eval()
    vocoder.eval()
    tts_ready = True
    print("[TTS-ES] Ready")

    # TTS EN
    print("[TTS] Loading FastPitch+HiFiGAN EN...")
    spec_gen_en = FastPitchModel.from_pretrained("tts_en_fastpitch_multispeaker")
    vocoder_en = HifiGanModel.from_pretrained("tts_en_hifitts_hifigan_ft_fastpitch")
    spec_gen_en.eval()
    vocoder_en.eval()
    loading_status["tts"] = "done"
    print("[TTS-EN] Ready")

    # LLM (via trtllm-serve sidecar)
    loading_status["llm"] = "loading"
    print(f"[LLM] Connecting to LLM at {LLM_BASE_URL} ({LLM_MODEL})...")
    llm_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="unused")

    # Wait for trtllm-serve to be ready + warmup with realistic prompts
    from openai import OpenAI
    sync_client = OpenAI(base_url=LLM_BASE_URL, api_key="unused", timeout=60)
    for attempt in range(60):
        try:
            # Simple chat — precompile basic generation kernels
            sync_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "hola cómo estás"}],
                max_tokens=min(50, LLM_MAX_TOKENS),
                temperature=0.3,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            print(f"[LLM] Warmup done (attempt {attempt + 1})")
            break
        except Exception as e:
            if attempt % 10 == 0:
                print(f"[LLM] Waiting for trtllm-serve... ({e.__class__.__name__})")
            time.sleep(5)
    else:
        print("[LLM] WARNING: trtllm-serve warmup failed after 5 min, continuing anyway")
    del sync_client
    loading_status["llm"] = "done"
    print("[LLM] Ready")

    # GPU warmup (STT + TTS)
    # CLAVE: el warmup corre DENTRO de los executors dedicados (_stt_executor /
    # _tts_executor) — los MISMOS threads que sirven requests reales. Asi el
    # handle de cuDNN se crea aca, en el arranque, y la 1ra transcripcion real
    # ya sale rapida (antes pagaba ~4s porque calentabamos en otro thread).
    loading_status["warmup"] = "loading"
    print("[INIT] GPU warmup (STT+TTS en thread dedicado)...")

    def _warmup_stt():
        # 1) forward sobre ruido → JIT-ea los kernels del modelo acustico.
        for _samps in (8000, 16000, 32000, 64000):
            noise = (np.random.randn(_samps).astype(np.float32) * 0.01)
            _stt_batch([(noise, None)])
        # 2) decode con tokens REALES → JIT-ea el path del tokenizer
        #    (ids_to_text / ids_to_lang / _detect_lang_words). El ruido produce
        #    tokens vacios y SALTEA este path, por eso la 1ra frase real pagaba
        #    el cold start del decode aunque el forward ya estuviera caliente.
        try:
            _dummy = list(range(5, 25))
            _ = stt_model.tokenizer.ids_to_text(_dummy)
            if hasattr(stt_model.tokenizer, "langs"):
                _ = stt_model.tokenizer.ids_to_lang(_dummy)
            _ = _detect_lang_words("hola cómo estás hello how are you")
        except Exception as e:
            print(f"[INIT] STT decode warmup skip: {e}")
        torch.cuda.synchronize()

    def _warmup_tts():
        with torch.no_grad():
            dt = spec_gen.parse("hola, esto es una prueba")
            ds = torch.tensor([SPEAKER_ID]).long().to(spec_gen.device)
            dsp = spec_gen.generate_spectrogram(tokens=dt, speaker=ds)
            vocoder.convert_spectrogram_to_audio(spec=dsp)
            dt_en = spec_gen_en.parse("hello, this is a test")
            ds_en = torch.tensor([0]).long().to(spec_gen_en.device)
            dsp_en = spec_gen_en.generate_spectrogram(tokens=dt_en, speaker=ds_en)
            vocoder_en.convert_spectrogram_to_audio(spec=dsp_en)
        torch.cuda.synchronize()

    _stt_t = time.time()
    _stt_executor.submit(_warmup_stt).result()
    print(f"[INIT] STT warmup (thread dedicado) {time.time() - _stt_t:.1f}s")
    _tts_executor.submit(_warmup_tts).result()
    loading_status["warmup"] = "done"

    print(f"[INIT] Ready in {time.time() - t0:.1f}s")
    models_ready = True


def clone_vad_model():
    """Create an independent VAD model instance from saved JIT buffer."""
    vad_model_buffer.seek(0)
    m = torch.jit.load(vad_model_buffer)
    m.eval()
    return m


# --- Pipeline functions ---

# Language detection by word matching (lexical) — complements ids_to_lang (acoustic)
_EN_WORDS = {
    "i", "you", "your", "my", "he", "she", "it", "we", "they", "them",
    "his", "her", "our", "its", "their", "this", "that", "these", "those",
    "is", "are", "was", "were", "am", "be", "have", "has", "had", "do",
    "does", "did", "will", "would", "could", "should", "can", "may", "might",
    "shall", "want", "need", "know", "think", "help", "give", "make", "take",
    "get", "go", "come", "see", "tell", "call", "speak", "talk", "work",
    "offer", "buy", "sell", "send", "pay", "like", "look", "find", "keep",
    "what", "how", "who", "where", "when", "why", "which",
    "and", "but", "or", "not", "for", "with", "from", "about", "into",
    "the", "of", "to", "in", "on", "at", "by", "up", "out", "if", "so",
    "name", "price", "plan", "service", "product", "company", "number",
    "good", "new", "much", "many", "more", "very", "also", "just", "only",
    "hello", "hi", "hey", "please", "thank", "thanks", "sorry", "yes",
    "yeah", "ok", "okay", "sure", "right", "well",
    "english", "spanish", "cloud", "customer", "agent", "support",
    "contact", "center", "advantage", "feature", "channel", "message",
}
_ES_WORDS = {
    "el", "la", "los", "las", "un", "una", "yo", "tu", "él", "ella",
    "nos", "ellos", "esto", "eso", "ese", "esa", "este", "esta",
    "es", "son", "fue", "era", "ser", "estar", "tiene", "hay", "puede",
    "hacer", "quiero", "necesito", "tengo", "dime", "haz", "dame",
    "hablar", "llamar", "saber", "creo", "digo", "voy", "poner",
    "qué", "cómo", "quién", "dónde", "cuándo", "cuál", "cuanto",
    "que", "por", "para", "con", "desde", "como", "donde", "cuando",
    "porque", "pero", "sin", "sobre", "entre", "hasta", "según",
    "nombre", "precio", "empresa", "número", "servicio", "producto",
    "bueno", "nuevo", "mucho", "más", "muy", "también", "solo",
    "hola", "gracias", "sí", "buenas", "buenos", "bien", "claro",
    "español", "inglés", "cliente", "agente", "ventaja", "canal",
}

def _detect_lang_words(text):
    """Detect language from transcribed text using word frequency."""
    words = set(text.lower().split())
    en = len(words & _EN_WORDS)
    es = len(words & _ES_WORDS)
    return "en" if en > es else "es"


def _stt_batch(batch):
    """Process a batch of STT requests on GPU. Called from executor thread.

    Pads audio arrays to the same length and runs a single batched forward().
    A batch of N takes ~1.2x the time of 1, vs N times when serialized.
    """
    audios = [b[0] for b in batch]
    n = len(batch)

    with torch.no_grad():
        max_len = max(len(a) for a in audios)
        batched = torch.zeros(n, max_len, device=stt_model.device)
        lengths = torch.zeros(n, dtype=torch.long, device=stt_model.device)
        for i, a in enumerate(audios):
            batched[i, :len(a)] = torch.tensor(a).float()
            lengths[i] = len(a)

        lp, el, _ = stt_model.forward(input_signal=batched, input_signal_length=lengths)

        # AggregateTokenizer (bilingual models) needs manual CTC decoding
        # because ctc_decoder_predictions_tensor fails with missing lang_id
        is_aggregate = hasattr(stt_model.tokenizer, 'langs')
        if is_aggregate:
            blank = lp.shape[-1] - 1
            results = []
            for i in range(n):
                greedy = lp[i, :el[i]].argmax(dim=-1)
                tokens = []
                prev = blank
                for t in greedy:
                    ti = t.item()
                    if ti != blank and ti != prev:
                        tokens.append(ti)
                    prev = ti
                text = stt_model.tokenizer.ids_to_text(tokens) if tokens else ""
                # Combined: acoustic (token IDs) OR lexical (word match)
                lang_a = stt_model.tokenizer.ids_to_lang(tokens) if tokens else "es"
                lang_w = _detect_lang_words(text) if text.strip() else "es"
                lang = "en" if (lang_a == "en" or lang_w == "en") else "es"
                results.append((text.strip(), lang))
        else:
            hyps = stt_model.decoding.ctc_decoder_predictions_tensor(lp, el)
            results = []
            for h in hyps:
                text = h.text if hasattr(h, "text") else str(h)
                results.append((text, "es"))
        return results


class STTBatchQueue:
    """Collects STT requests and processes them in GPU batches.

    Instead of serializing STT with a lock (one at a time),
    this queue collects multiple audio inputs within a time window
    and runs them as a single padded batch through the model.
    """

    def __init__(self, max_batch=16, max_wait_ms=30):
        self.max_batch = max_batch
        self.max_wait_ms = max_wait_ms
        self._queue = asyncio.Queue()
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._worker_loop())

    async def transcribe(self, audio_float32):
        """Submit audio and wait for transcription result. Returns (text, lang) or None."""
        if len(audio_float32) < 1600:  # < 0.1s
            return None
        future = asyncio.get_event_loop().create_future()
        await self._queue.put((audio_float32, future))
        return await future

    async def _worker_loop(self):
        """Background worker: collect requests, process as GPU batches."""
        loop = asyncio.get_event_loop()
        while True:
            batch = [await self._queue.get()]

            # Collect more within time window
            deadline = loop.time() + self.max_wait_ms / 1000
            while len(batch) < self.max_batch:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            n = len(batch)
            t0 = time.time()
            try:
                results = await loop.run_in_executor(_stt_executor, _stt_batch, batch)
                elapsed = time.time() - t0
                if n > 1:
                    print(f"[STT-BATCH] {n} items in {elapsed:.3f}s ({elapsed/n:.3f}s/item)")
                for (_, future), result in zip(batch, results):
                    if not future.done():
                        future.set_result(result)
            except Exception as e:
                print(f"[STT-BATCH] Error processing batch of {n}: {e}")
                for _, future in batch:
                    if not future.done():
                        future.set_exception(e)


def _get_speaker(speaker_id, lang="es"):
    """Get or create cached speaker tensor."""
    cache = _speaker_cache_en if lang == "en" else _speaker_cache
    device = spec_gen_en.device if lang == "en" else spec_gen.device
    if speaker_id not in cache:
        cache[speaker_id] = torch.tensor([speaker_id]).long().to(device)
    return cache[speaker_id]


def do_tts(sentence, speaker_id=50, lang="es"):
    """Synthesize with NeMo FastPitch+HiFiGAN (local GPU)."""
    sg = spec_gen_en if lang == "en" else spec_gen
    vc = vocoder_en if lang == "en" else vocoder
    parsed = sg.parse(sentence)
    spectrogram = sg.generate_spectrogram(tokens=parsed, speaker=_get_speaker(speaker_id, lang))
    audio = vc.convert_spectrogram_to_audio(spec=spectrogram)
    audio_np = audio.squeeze().detach().cpu().numpy()
    buf = io.BytesIO()
    sf.write(buf, audio_np, 44100, format="WAV")
    return buf.getvalue()


async def generate_sentences(user_text, system_prompt=None, temperature=0.3, history=None, max_tokens=None, lang="es", append_user=True):
    """Stream LLM response via trtllm-serve, yield complete sentences.

    append_user: si True (default), agrega {role:user, content:user_text} al final
    de messages. Se pone False cuando el caller ya inyectó el user en history
    (iteraciones 2+ del tool loop) para evitar duplicar el turno del usuario.
    """
    effective_prompt = system_prompt or SYSTEM_PROMPT
    temp = max(0.01, min(1.0, temperature))
    tok = max_tokens or LLM_MAX_TOKENS
    print(f"[LLM] System prompt: {effective_prompt[:120]}...")
    print(f"[LLM] User text: {user_text} | temp={temp} | max_tokens={tok} | history={len(history) if history else 0} msgs | append_user={append_user}")

    messages = [{"role": "system", "content": effective_prompt}]
    if history:
        messages.extend(history)
    if append_user:
        messages.append({"role": "user", "content": user_text})

    create_kwargs = dict(
        model=LLM_MODEL,
        messages=messages,
        temperature=temp,
        max_tokens=tok,
        top_p=LLM_TOP_P,
        frequency_penalty=LLM_FREQ_PENALTY,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    stream = await llm_client.chat.completions.create(**create_kwargs)

    buffer = ""
    # Filtrar bloques de thinking: Qwen3 usa <think>...</think>; con enable_thinking=True
    # algunos modelos empiezan a pensar desde el primer token (sin opening tag).
    _thinking_enabled = create_kwargs.get("extra_body", {}).get("chat_template_kwargs", {}).get("enable_thinking", False)
    in_think = _thinking_enabled  # si thinking ON, asumir que empieza pensando
    ends = {".", "!", "?", "。", ":", "\n"}
    async for chunk in stream:
        delta = chunk.choices[0].delta
        content = delta.content or ""
        buffer += content

        # Filtrar bloques <think>...</think> (reasoning del LLM, no debe llegar al TTS)
        if "<think>" in buffer and not in_think:
            # Guardar texto antes del <think> y descartar el resto
            pre_think = buffer[:buffer.index("<think>")]
            buffer = pre_think
            in_think = True
            continue
        if in_think:
            if "</think>" in buffer:
                # Descartar todo hasta </think> y continuar con lo que sigue
                post_think = buffer[buffer.index("</think>") + len("</think>"):]
                buffer = post_think
                in_think = False
                if not buffer.strip():
                    continue
            else:
                buffer = ""
                continue

        while True:
            found = False
            for i, c in enumerate(buffer):
                if c in ends:
                    if c == ".":
                        # "." after digit at end of buffer: wait for more tokens
                        if i > 0 and buffer[i-1].isdigit() and i == len(buffer) - 1:
                            break
                        # "." between digits (thousands: 13.800, decimals: 3.14)
                        if i > 0 and i < len(buffer) - 1 and buffer[i-1].isdigit() and buffer[i+1].isdigit():
                            continue
                    s = buffer[: i + 1].strip()
                    buffer = buffer[i + 1 :]
                    if s:
                        yield _clean(s, lang)
                    found = True
                    break
            if not found:
                break

    if buffer.strip():
        yield _clean(buffer.strip(), lang)


def _make_num_to_words(lang="es"):
    """Return a num-to-words replacer for the given language."""
    def _num_to_words(match):
        s = match.group(0)
        if lang == "es":
            # Remove thousands separator dots (13.800 → 13800)
            s = re.sub(r"(\d)\.(\d{3})(?!\d)", r"\1\2", s)
            if "," in s:  # decimal comma: 3,14
                s = s.replace(",", ".")
        else:
            # EN uses commas for thousands (13,800 → 13800)
            s = s.replace(",", "")
        try:
            n = float(s) if "." in s else int(s)
            return num2words.num2words(n, lang=lang)
        except Exception:
            return match.group(0)
    return _num_to_words

def _clean(text, lang="es"):
    """Strip markdown and normalize numbers for TTS."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"[*_#~`]", "", text)
    # Strip leading list markers (- , • , etc.) that TTS tries to pronounce
    text = re.sub(r"^\s*[-–—•▪▸◦●○]\s+", "", text)
    # Normalize time formats: 10am → "10 de la mañana", 2pm → "2 de la tarde"
    _am = "de la mañana" if lang == "es" else "a m"
    _pm = "de la tarde" if lang == "es" else "p m"
    text = re.sub(r"(\d{1,2}(?::\d{2})?)\s*([aApP]\.?[mM]\.?)", lambda m: f"{m.group(1)} {_am if m.group(2)[0].lower() == 'a' else _pm}", text)
    # Expand common abbreviations
    text = re.sub(r"\betc\.", "etcétera" if lang == "es" else "et cetera", text)
    # Convert numbers to words (handles 13.800, 3,14, 42, etc.)
    text = re.sub(r"\d[\d.,]*\d|\d", _make_num_to_words(lang), text)
    return text.strip()


# --- TTS Batching Queue ---

class TTSBatchQueue:
    """Collects TTS requests and processes them in GPU batches.

    Instead of running HiFiGAN one sentence at a time (serialized),
    this queue collects multiple requests within a time window and
    processes them as a single GPU batch. HiFiGAN supports batch
    dimension natively, so N spectrograms take ~1.2x the time of 1.
    """

    def __init__(self, max_batch=8, max_wait_ms=15):
        self.max_batch = max_batch
        self.max_wait_ms = max_wait_ms
        self._queue = asyncio.Queue()
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._worker_loop())

    async def synthesize(self, text, speaker_id, lang="es"):
        """Submit a TTS request and wait for the result (WAV bytes)."""
        future = asyncio.get_event_loop().create_future()
        await self._queue.put((text, speaker_id, lang, future))
        return await future

    async def _worker_loop(self):
        """Background worker: collect requests, process as GPU batches."""
        loop = asyncio.get_event_loop()
        while True:
            # Wait for at least one request
            batch = [await self._queue.get()]

            # Collect more within time window
            deadline = loop.time() + self.max_wait_ms / 1000
            while len(batch) < self.max_batch:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            n = len(batch)
            t0 = time.time()
            try:
                results = await loop.run_in_executor(_tts_executor, _tts_batch, batch)
                elapsed = time.time() - t0
                if n > 1:
                    print(f"[TTS-BATCH] {n} items in {elapsed:.3f}s ({elapsed/n:.3f}s/item)")
                for item, result in zip(batch, results):
                    future = item[-1]
                    if not future.done():
                        future.set_result(result)
            except Exception as e:
                print(f"[TTS-BATCH] Error processing batch of {n}: {e}")
                for item in batch:
                    future = item[-1]
                    if not future.done():
                        future.set_exception(e)


def _tts_batch(batch):
    """Process a batch of TTS requests on GPU. Called from executor thread.

    Strategy: run FastPitch individually (fast, avoids token padding issues),
    then pad spectrograms and run HiFiGAN as a single batch (heavy, benefits
    most from GPU parallelism). Splits batch by language (ES/EN use different models).
    """
    texts = [b[0] for b in batch]
    speakers = [b[1] for b in batch]
    langs = [b[2] for b in batch]
    n = len(batch)

    with torch.no_grad():
        if n == 1:
            return [do_tts(texts[0], speakers[0], langs[0])]

        # Split by language — each sub-batch uses its own models
        results = [None] * n
        for target_lang in ("es", "en"):
            indices = [i for i in range(n) if langs[i] == target_lang]
            if not indices:
                continue

            sg = spec_gen_en if target_lang == "en" else spec_gen
            vc = vocoder_en if target_lang == "en" else vocoder

            # Step 1: FastPitch individually
            specs = []
            spec_lens = []
            for i in indices:
                parsed = sg.parse(texts[i])
                speaker = _get_speaker(speakers[i], target_lang)
                sp = sg.generate_spectrogram(tokens=parsed, speaker=speaker)
                specs.append(sp.squeeze(0))
                spec_lens.append(sp.shape[-1])

            if len(indices) == 1:
                # Single item in this language
                audio = vc.convert_spectrogram_to_audio(spec=specs[0].unsqueeze(0))
                audio_np = audio.squeeze().detach().cpu().numpy()
                buf = io.BytesIO()
                sf.write(buf, audio_np, 44100, format="WAV")
                results[indices[0]] = buf.getvalue()
                continue

            # Step 2: Pad spectrograms for batched HiFiGAN
            max_mel = max(spec_lens)
            C = specs[0].shape[0]
            batched_specs = torch.zeros(len(indices), C, max_mel, device=specs[0].device)
            for j, sp in enumerate(specs):
                batched_specs[j, :, :spec_lens[j]] = sp

            # Step 3: HiFiGAN batch
            audios = vc.convert_spectrogram_to_audio(spec=batched_specs)

            # Step 4: Trim padding and encode each as WAV
            audio_ratio = audios.shape[-1] / max_mel
            for j, idx in enumerate(indices):
                trim_len = int(spec_lens[j] * audio_ratio)
                audio_np = audios[j, :trim_len].cpu().numpy()
                buf = io.BytesIO()
                sf.write(buf, audio_np, 44100, format="WAV")
                results[idx] = buf.getvalue()

        return results


# --- WebSocket pipeline ---

async def run_streaming_pipeline(*args, **kwargs):
    """Wrapper: el pipeline corre como task fire-and-forget (asyncio.create_task).
    Si el cliente se desconecta a mitad de stream, los `ws.send_json` lanzan
    WebSocketDisconnect/ConnectionClosed; sin este guard la excepcion queda
    'never retrieved' y ensucia el loop. Atrapamos TODO aqui para que la task
    siempre termine limpia y nunca afecte al servidor."""
    try:
        await _run_streaming_pipeline(*args, **kwargs)
    except WebSocketDisconnect:
        print("[PIPELINE] cliente desconectado a mitad de stream (ignorado)")
    except Exception as e:
        import traceback
        print(f"[PIPELINE] error no fatal: {e}")
        traceback.print_exc()


async def _run_streaming_pipeline(ws, audio_float32, speaker_id, system_prompt,
                                 temperature, history,
                                 text_override=None, max_tokens=None, speaker_en=0):
    """S2S pipeline.

    Takes float32 numpy audio from VAD buffer (16kHz mono, no ffmpeg needed).
    LLM via trtllm-serve (async), STT/TTS via NeMo (batch queue).
    If text_override is set, skip STT (used for load testing).
    """
    t0 = time.time()

    lang = "es"  # default language

    if text_override:
        text = text_override
    else:
        duration_s = len(audio_float32) / 16000
        print(f"[PIPELINE] Audio: {len(audio_float32)} samples ({duration_s:.2f}s)")

        # STT (batched GPU inference via queue)
        await ws.send_json({"type": "status", "message": "Transcribiendo..."})
        result = await stt_queue.transcribe(audio_float32)

        # Unpack (text, lang) tuple from bilingual STT
        if isinstance(result, tuple):
            text, lang = result
        else:
            text = result

        if not text:
            await ws.send_json({"type": "error", "message": "No se detectó habla."})
            return

        stt_time = time.time() - t0
        print(f"[PIPELINE] STT: '{text}' lang={lang} ({stt_time:.2f}s)")
        await ws.send_json({"type": "stt", "text": text, "lang": lang, "time": round(stt_time, 2)})


    await ws.send_json({"type": "status", "message": "Pensando..."})

    # LLM streaming (async) + TTS per sentence. Community has no tools, so this is a
    # single generation pass: every sentence the LLM produces is spoken via TTS.
    t_first = None
    n = 0
    full_response = []

    async for sentence in generate_sentences(
        text, system_prompt, temperature, history or [],
        max_tokens=max_tokens, lang=lang,
        append_user=True,
    ):
        n += 1
        full_response.append(sentence)
        sid = speaker_en if lang == "en" else speaker_id
        wav_bytes = await tts_queue.synthesize(sentence, sid, lang=lang)
        audio_b64 = base64.b64encode(wav_bytes).decode()
        if t_first is None:
            t_first = time.time()
        try:
            await ws.send_json(
                {"type": "audio", "sentence": sentence, "number": n, "data": audio_b64}
            )
        except Exception:
            return

    # Update conversation history
    if history is not None:
        history.append({"role": "user", "content": text})
        if full_response:
            history.append({"role": "assistant", "content": " ".join(full_response)})
        while len(history) > 30:
            history.pop(0)

    total = time.time() - t0
    ttfa = round(t_first - t0, 2) if t_first else None

    try:
        await ws.send_json({"type": "done", "ttfa": ttfa, "total": round(total, 2)})
    except Exception:
        pass


# ─── SSRF protection ─────────────────────────────────────────────────────────
# When the server fetches user-supplied URLs (e.g. /api/documents/url) we must
# refuse:
#   · Non-http(s) schemes (file://, gopher://, etc.)
#   · Loopback IPs (own services like vLLM on 127.0.0.1:8002)
#   · Link-local (169.254.0.0/16 — AWS/GCP metadata if we ever migrate)
# RFC1918 private nets (10/8, 172.16/12, 192.168/16) are NOT blocked: Docker
# container-to-container traffic on RunPod relies on those ranges.
import ipaddress
import socket
from urllib.parse import urlparse

_SSRF_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]


def _ssrf_check_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Rejects bad schemes and loopback/link-local IPs."""
    if not url or len(url) > 2048:
        return False, "empty or oversize URL"
    try:
        p = urlparse(url)
    except Exception:
        return False, "malformed URL"
    if p.scheme not in ("http", "https"):
        return False, f"scheme {p.scheme!r} not allowed (only http/https)"
    host = p.hostname
    if not host:
        return False, "no hostname"
    # Resolve every A/AAAA record; reject if ANY resolves to a blocked range.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "DNS resolution failed"
    for fam, _, _, _, sa in infos:
        ip_str = sa[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for net in _SSRF_BLOCKED_NETS:
            if ip in net:
                return False, f"resolved IP {ip} is in blocked range {net}"
    return True, "ok"


def _extract_token(request: Request, key: str | None = None) -> str:
    """Extract auth token. Preferred order:

    1. Session cookie (``nemo_session``)         — browser path, after /api/auth/login
    2. ``Authorization: Bearer <token>`` header  — CLI / machine clients
    3. ``X-Api-Key: <token>`` header             — CLI / machine clients

    Query-param ``?key=`` is rejected by middleware before reaching handlers,
    so it never appears here. The ``key`` argument is kept on handler
    signatures only to avoid churn; it is always ``None`` in practice.
    """
    cookie_tok = request.cookies.get(SESSION_COOKIE)
    api_key_from_cookie = _resolve_session(cookie_tok)
    if api_key_from_cookie:
        return api_key_from_cookie
    bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if bearer:
        return bearer
    api_hdr = request.headers.get("x-api-key", "").strip()
    if api_hdr:
        return api_hdr
    return ""


# Community edition is single-tenant. All config lives under one fixed scope in the
# (multi-tenant-capable) schema; there are no per-customer tenants or per-tenant keys.
# Only the master API_KEY authenticates, and it maps to this single scope. The Realtime
# settings panel persists into CONFIG_SCOPE's config (an overlay on top of server_config),
# so server_config is only changed from the admin "Server Config" panel (PUT /api/config).
CONFIG_SCOPE = "__admin__"


async def _resolve_role(token: str) -> dict | None:
    """Validate the token. Returns {"role": "admin"} if authorized, else None.

    Community is single-tenant: the only credential is the master API_KEY. If no
    API_KEY is configured (dev only), access is open.
    """
    if not token:
        return {"role": "admin"} if not API_KEY else None
    if token == API_KEY:
        return {"role": "admin"}
    return None


# --- Routes ---

@limiter.limit(RL_AUTH)
@app.post("/api/auth")
async def api_auth(request: Request):
    """Return role info for the *current* session (if any).

    This endpoint is idempotent: it does NOT log the client in. It only
    inspects the existing session cookie / Authorization header. Use
    POST /api/auth/login to establish a session.
    """
    token = _extract_token(request)
    role = await _resolve_role(token)
    if not role:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return role


@limiter.limit(RL_AUTH)
@app.post("/api/auth/login")
async def api_auth_login(request: Request, payload: dict = Body(default=None)):
    """Validate the API key and set an HttpOnly session cookie.

    Body: {"key": "<api_key>"}

    On success: returns the role and Set-Cookies a server-side opaque session
    token. The raw key never sees a server log or browser history.
    """
    key = ""
    if isinstance(payload, dict):
        key = (payload.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    role = await _resolve_role(key)
    if not role:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token = _new_session_token(key)
    response = JSONResponse(role)
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    """Revoke the current session and clear the cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    _revoke_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/health")
async def health(request: Request, key: str = Query(default=None)):
    token = _extract_token(request, key)
    if not await _check_any_key(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"ready": models_ready, "tts_ready": tts_ready, "models": loading_status}


@app.get("/settings")
async def get_settings(request: Request, key: str = Query(default=None)):
    token = _extract_token(request, key)
    if not await _check_any_key(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Return server config with the single scope's own values on top.
    # system_prompt shows ONLY the scope's own prompt (not the global concatenation).
    if db:
        server_cfg = {e.key: e.value for e in await db.get_all_config()}
        scope_cfg = await db.get_tenant_config(CONFIG_SCOPE)
        raw = {**server_cfg, **scope_cfg}
        raw["system_prompt"] = scope_cfg.get("system_prompt", "")
        result = {}
        for k, v in raw.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                result[k] = v
        return result
    return await load_settings()


_EN_SPEAKER_IDS = {92, 6097, 6670, 6671, 8051, 9017, 9136, 11614, 11697, 12787}
_ES_SPEAKER_RANGE = range(0, 174)  # 0-173 inclusive


def _detect_lang(speaker_id: int):
    """Return 'en' or 'es' for valid speaker_id, or None if invalid."""
    if speaker_id in _EN_SPEAKER_IDS:
        return "en"
    if speaker_id in _ES_SPEAKER_RANGE:
        return "es"
    return None


def _sanitize_tts_text(text: str, max_len: int = 250) -> str:
    import unicodedata
    cleaned = "".join(c for c in text if unicodedata.category(c)[0] != "C" or c in (" ", "\n"))
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_len]


@app.get("/test_voice/{speaker_id}")
async def test_voice(speaker_id: int, request: Request, key: str = Query(default=None),
                     text: str = Query(default=None)):
    token = _extract_token(request, key)
    if not await _check_any_key(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not tts_ready:
        return JSONResponse({"error": "TTS aún cargando..."}, status_code=503)
    lang = _detect_lang(speaker_id)
    if lang is None:
        return JSONResponse({
            "error": "speaker_id inválido",
            "valid_es": "0-173",
            "valid_en": sorted(_EN_SPEAKER_IDS),
        }, status_code=400)
    raw_text = text or ("Hello, I'm happy to help you today." if lang == "en" else "Hola, me encanta poder ayudarte hoy.")
    tts_text = _sanitize_tts_text(raw_text)
    if not tts_text:
        return JSONResponse({"error": "text vacío después de sanitización"}, status_code=400)
    wav_bytes = await tts_queue.synthesize(tts_text, speaker_id, lang=lang)
    audio_b64 = base64.b64encode(wav_bytes).decode()
    return {"audio": audio_b64, "lang": lang, "speaker_id": speaker_id}


@limiter.limit(RL_TENANTS)
@app.get("/api/config")
async def api_get_config(request: Request, key: str = Query(default=None)):
    """Admin: read all server config."""
    token = _extract_token(request, key)
    if not _check_api_key(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = await load_settings()
    # Community: surface the built-in base prompt so the Server field shows the
    # actual default (editable), not just a placeholder, on a fresh install.
    if not settings.get("system_prompt"):
        settings["system_prompt"] = SYSTEM_PROMPT
    return settings


@limiter.limit(RL_TENANTS)
@app.put("/api/config")
async def api_put_config(request: Request, key: str = Query(default=None)):
    """Admin: update server config (partial update)."""
    token = _extract_token(request, key)
    if not _check_api_key(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    for k, v in body.items():
        await db.set_config(k, v if isinstance(v, str) else json.dumps(v))
    return await load_settings()


@app.get("/")
async def index():
    with open(os.path.join(_APP_DIR, "static/index.html")) as f:
        return HTMLResponse(f.read())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global _active_connections
    from modules.vad import StreamingVAD

    # Auth: cookie-based session (browser) or Authorization/X-Api-Key header
    # (CLI). Query-string keys are not accepted on WebSocket either.
    if "key" in ws.query_params:
        await ws.accept()
        await ws.close(code=4001, reason="api_key in URL not allowed")
        return

    cookie_tok = ws.cookies.get(SESSION_COOKIE)
    token = _resolve_session(cookie_tok) or ""
    if not token:
        bearer = (ws.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        token = bearer or ws.headers.get("x-api-key", "")

    if not await _check_any_key(token):
        await ws.accept()
        await ws.close(code=4001, reason="unauthorized")
        return

    # Authenticated above (gate at _check_any_key). Community is single-tenant, so
    # all config reads/writes use the single CONFIG_SCOPE.

    # Origin check
    origin = ws.headers.get("origin", "")
    if not _check_origin(origin):
        await ws.accept()
        await ws.close(code=4003, reason="origin not allowed")
        return

    # Max connections
    with _connections_lock:
        if MAX_CONNECTIONS > 0 and _active_connections >= MAX_CONNECTIONS:
            await ws.accept()
            await ws.send_json({"type": "error", "message": "Too many connections"})
            await ws.close(code=4029, reason="too many connections")
            return
        _active_connections += 1
    print(f"[WS] Connected ({_active_connections}/{MAX_CONNECTIONS or '∞'})")

    await ws.accept()
    # Load effective config: server_config + the single scope's overlay.
    if db:
        saved = await db.get_effective_config(CONFIG_SCOPE)
        for k, v in saved.items():
            try:
                saved[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                pass
    else:
        saved = await load_settings()
    speaker_id = saved.get("speaker", SPEAKER_ID)
    speaker_en = saved.get("speaker_en", 0)
    system_prompt = saved.get("system_prompt")
    temperature = saved.get("temperature", 0.3)
    max_tokens = saved.get("max_tokens", LLM_MAX_TOKENS)
    silence_ms = saved.get("silence_ms", 600)
    vad_threshold = saved.get("threshold", 0.5)
    conversation_history = []
    muted = False
    call_channel_id = None          # set when a SIP/telephony bridge sends call_context

    # Per-connection VAD instance (cloned model to isolate RNN state)
    conn_vad_model = clone_vad_model() if vad_model_buffer else None
    vad = StreamingVAD(conn_vad_model, threshold=vad_threshold, silence_threshold_ms=silence_ms) if conn_vad_model else None

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            # JSON message: config, mode, VAD config, voice test
            if "text" in msg and msg["text"]:
                data = json.loads(msg["text"])

                if data.get("type") == "ping":
                    continue

                elif data.get("type") == "call_context":
                    # a SIP/telephony bridge reports the real Asterisk channelId
                    # Inyectar en system_prompt para que el LLM use el ID real
                    call_channel_id = data.get("channel_id")
                    if call_channel_id and system_prompt:
                        system_prompt = system_prompt.replace("CALLER_CHANNEL", call_channel_id)
                    print(f"[WS] Call context: channel_id={call_channel_id} → injected into system_prompt")

                elif data.get("type") == "clear_history":
                    conversation_history.clear()
                    print("[WS] Conversation history cleared")
                    await ws.send_json({"type": "history_cleared"})

                elif data.get("type") == "vad_config":
                    if "silence_ms" in data:
                        silence_ms = int(data["silence_ms"])
                    if "threshold" in data:
                        vad_threshold = float(data["threshold"])
                    if conn_vad_model:
                        vad = StreamingVAD(conn_vad_model, threshold=vad_threshold, silence_threshold_ms=silence_ms)
                    # Persistir SOLO los campos VAD (Fase 2 — no mass-write como antes)
                    if "silence_ms" in data and db:
                        await db.set_tenant_config(CONFIG_SCOPE, "silence_ms", str(silence_ms))
                    if "threshold" in data and db:
                        await db.set_tenant_config(CONFIG_SCOPE, "threshold", str(vad_threshold))
                    print(f"[WS] VAD silence: {silence_ms}ms, threshold: {vad_threshold}")

                elif data.get("type") == "mute":
                    muted = data.get("muted", False)
                    if muted and vad:
                        vad.reset()
                    print(f"[WS] Muted: {muted}")

                elif data.get("type") == "chat":
                    # Text chat: bypass VAD+STT, send text directly to LLM+TTS
                    text = data.get("text", "hola")
                    if not models_ready:
                        await ws.send_json({"type": "error", "message": "Modelos cargando..."})
                        continue
                    # Fake STT result
                    await ws.send_json({"type": "stt", "text": text, "time": 0.0})
                    asyncio.create_task(
                        run_streaming_pipeline(
                            ws, None, speaker_id, system_prompt,
                            temperature, conversation_history,
                            text_override=text, max_tokens=max_tokens, speaker_en=speaker_en,
                        )
                    )

                elif data.get("type") == "test_voice":
                    sid = int(data.get("speaker", speaker_id))
                    test_text = "Hola, me encanta poder ayudarte hoy."
                    if not tts_ready:
                        await ws.send_json({"type": "error", "message": "TTS aún cargando..."})
                        continue
                    wav_bytes = await tts_queue.synthesize(test_text, sid)
                    audio_b64 = base64.b64encode(wav_bytes).decode()
                    await ws.send_json({"type": "voice_preview", "data": audio_b64})

                else:
                    # Config update (speaker, system_prompt, temperature, max_tokens)
                    # Fase 3.A: persistir SOLO los campos cuyo valor difiere del DB actual.
                    # Esto previene el bug de mass-overwrite cuando el frontend manda valores stale.

                    # Leer estado actual de DB (1 query)
                    if db:
                        _db_current = await db.get_tenant_config(CONFIG_SCOPE)
                    else:
                        _db_current = {}

                    persisted = []

                    async def _write_if_changed(key, new_value):
                        """Persiste solo si el nuevo valor difiere del DB."""
                        if not db:
                            return
                        new_str = str(new_value)
                        if _db_current.get(key) == new_str:
                            return  # mismo valor, skip
                        await db.set_tenant_config(CONFIG_SCOPE, key, new_str)
                        persisted.append(key)

                    if "speaker" in data:
                        speaker_id = int(data["speaker"])
                        await _write_if_changed("speaker", speaker_id)
                    if "speaker_en" in data:
                        speaker_en = int(data["speaker_en"])
                        await _write_if_changed("speaker_en", speaker_en)
                    if "temperature" in data:
                        temperature = float(data["temperature"])
                        await _write_if_changed("temperature", temperature)
                    if "max_tokens" in data:
                        requested = int(data["max_tokens"])
                        # Cap against server's max_tokens (hard ceiling).
                        # Mirrors the cascade rule in get_effective_config:
                        # tenant can only override DOWNWARDS.
                        server_max = None
                        if db:
                            try:
                                server_max = int((await db.get_config("max_tokens")) or "0") or None
                            except (ValueError, TypeError):
                                server_max = None
                        max_tokens = min(requested, server_max) if server_max else requested
                        # Persist the value the tenant actually got (capped),
                        # so the next session reads the same effective number.
                        await _write_if_changed("max_tokens", max_tokens)
                    if "system_prompt" in data:
                        # The Realtime form persists into CONFIG_SCOPE's config — it
                        # NEVER touches server_config. To change the global server
                        # prompt, use the "Server Config" panel (PUT /api/config).
                        _new_prompt = data["system_prompt"].strip() or None
                        if db and _new_prompt:
                            server_cfg = {e.key: e.value for e in await db.get_all_config()}
                            server_sp = server_cfg.get("system_prompt", "")
                            system_prompt = (server_sp + "\n\n" + _new_prompt).strip()
                        else:
                            system_prompt = _new_prompt
                        if _new_prompt is not None:
                            await _write_if_changed("system_prompt", _new_prompt)

                    print(f"[WS] Config update: persisted={persisted}")

            # Binary message: PCM chunks -> VAD
            elif "bytes" in msg and msg["bytes"]:
                if muted or not vad:
                    continue

                result = vad.process_chunk(msg["bytes"])
                if result is None:
                    continue

                if result["event"] == "speech_start":
                    print("[VAD] Speech started")
                    await ws.send_json({"type": "vad", "speaking": True})

                elif result["event"] == "speech_end":
                    audio = result["audio"]
                    print(f"[VAD] Speech ended: {len(audio)} samples ({len(audio)/16000:.2f}s), max_amp={float(np.max(np.abs(audio))):.4f}")
                    await ws.send_json({"type": "vad", "speaking": False})

                    if not models_ready:
                        await ws.send_json({"type": "error", "message": "Modelos aún cargando, espera..."})
                        continue

                    # Launch pipeline as async task (non-blocking, allows reading more chunks)
                    asyncio.create_task(
                        run_streaming_pipeline(
                            ws, result["audio"], speaker_id, system_prompt,
                            temperature, conversation_history,
                            max_tokens=max_tokens, speaker_en=speaker_en,
                        )
                    )

                elif result["event"] == "speech_too_short":
                    print("[VAD] Speech too short (filtered)")
                    await ws.send_json({"type": "vad", "speaking": False})

    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        with _connections_lock:
            _active_connections = max(0, _active_connections - 1)


@app.on_event("startup")
async def startup():
    global tts_queue, stt_queue, db, rag_queue
    db = ConfigStore()
    await db.initialize()
    print(f"[CONFIG] Loaded from {db.path}")

    # RAG is a Pro feature — disabled in Community edition
    rag_queue = None

    tts_queue = TTSBatchQueue(max_batch=16, max_wait_ms=15)
    tts_queue.start()
    print("[TTS-BATCH] Queue started (max_batch=16, max_wait=15ms)")

    stt_queue = STTBatchQueue(max_batch=32, max_wait_ms=15)
    stt_queue.start()
    print("[STT-BATCH] Queue started (max_batch=32, max_wait=15ms)")

    # Check WebSocket support
    ws_lib = None
    try:
        import websockets
        ws_lib = f"websockets {websockets.__version__}"
    except ImportError:
        try:
            import wsproto
            ws_lib = f"wsproto {wsproto.__version__}"
        except ImportError:
            ws_lib = None
    if ws_lib:
        print(f"[INIT] WebSocket: {ws_lib}")
    else:
        print("[INIT] WARNING: No WebSocket library! Install: pip install websockets")

    # Security info
    auth_mode = "API_KEY" if API_KEY else "DISABLED"
    print(f"[SECURITY] Auth: {auth_mode}, Max connections: {MAX_CONNECTIONS or 'unlimited'}, "
          f"Allowed origins: {ALLOWED_ORIGINS or 'all'}")

    # Models are loaded in __main__ (true main thread, before the asyncio loop) — NeMo
    # Conformer .from_pretrained deadlocks inside a running event loop on aarch64 (GH200/DGX Spark).


# ---------------------------------------------------------------------------
# /v1/realtime — OpenAI Realtime API (GA) compatible endpoint (self-contained module)
# Migrates to nemo-rt-pro by copying realtime_api.py + this wiring block.
# ---------------------------------------------------------------------------
from types import SimpleNamespace as _SimpleNamespace
from realtime_api import register_realtime as _register_realtime
from modules.vad import StreamingVAD as _RT_StreamingVAD


def _rt_make_vad(threshold=0.5, silence_ms=600):
    m = clone_vad_model() if vad_model_buffer else None
    return _RT_StreamingVAD(m, threshold=threshold, silence_threshold_ms=silence_ms) if m else None


async def _rt_get_settings():
    if db:
        saved = await db.get_effective_config(CONFIG_SCOPE)
        for _k, _v in list(saved.items()):
            try:
                saved[_k] = json.loads(_v)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return saved
    return await load_settings()


_realtime_deps = _SimpleNamespace(
    check_key=_check_any_key,
    resolve_session=_resolve_session,
    session_cookie=SESSION_COOKIE,
    make_vad=_rt_make_vad,
    transcribe=lambda a: stt_queue.transcribe(a),
    generate_sentences=generate_sentences,
    synthesize=lambda text, sid, lang: tts_queue.synthesize(text, sid, lang),
    get_settings=_rt_get_settings,
    models_ready=lambda: models_ready,
    default_speaker=SPEAKER_ID,
)
_register_realtime(app, _realtime_deps)


if __name__ == "__main__":
    import uvicorn

    load_models()  # true main thread, before asyncio loop starts (aarch64-safe; x86 OK too)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info",
                ws="websockets", ws_ping_interval=20, ws_ping_timeout=20,
                forwarded_allow_ips="*", proxy_headers=True)
