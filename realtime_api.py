"""
realtime_api.py — Self-contained, OpenAI Realtime API (GA) compatible /v1/realtime endpoint.

Goal: emulate OpenAI's Realtime WebSocket protocol so any OpenAI client/SDK/bridge
(e.g. asterisk_to_openai_rt) can point at Nemo-RT with a 1-line URL change and work.

This is a TRANSLATION layer over Nemo's existing pipeline (VAD -> STT -> LLM -> TTS),
NOT a rewrite. It reaches the pipeline through an injected `deps` object, so the whole
feature lives in ONE file and migrates to nemo-rt-pro by copying this file + wiring deps.

Wire it in with:  register_realtime(app, deps)

Transport = WebSocket at /v1/realtime. Audio formats implemented in the codec
(stdlib audioop): audio/pcmu (g711 mu-law 8kHz, telephony — validated on a live
SIP call), audio/pcm (linear 16-bit, negotiable rate, default 24kHz — used by the
browser UI and SDK clients), and audio/pcma (g711 a-law).

Audio conversion uses only the stdlib `audioop` (mu-law + resample) so there are no
extra dependencies.
"""

import asyncio
import base64
import io
import json
import time
import uuid
import wave

from starlette.websockets import WebSocket  # FastAPI needs the param type-annotated

try:
    import audioop  # stdlib (removed in Python 3.13)
except ImportError:  # pragma: no cover
    try:
        import audioop_lts as audioop  # pip install audioop-lts  (Python 3.13+)
    except ImportError:
        audioop = None  # codec will raise a clear error if used


# ---------------------------------------------------------------------------
# IDs (OpenAI-style opaque identifiers)
# ---------------------------------------------------------------------------
def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Audio codec: wire format  <->  Nemo internal PCM16
#   IN : client wire audio  -> PCM16 mono 16kHz  (what VAD/STT consume)
#   OUT: TTS WAV (44.1kHz)   -> client wire audio (what the client plays)
# Nemo internal rates: VAD/STT = 16kHz, TTS output = 44.1kHz.
# ---------------------------------------------------------------------------
NEMO_STT_RATE = 16000
VAD_FRAME_BYTES = 1024  # Silero VAD frame @16kHz = 512 samples * 2 bytes (int16)

_WIRE_RATE = {
    "audio/pcmu": 8000,
    "audio/pcma": 8000,
    "audio/pcm": 24000,   # OpenAI's linear PCM is 24kHz
}


class AudioCodec:
    """Stateful converter for one direction pair. Keeps ratecv filter state so a
    continuous stream resamples cleanly across chunks."""

    def __init__(self, in_fmt: str = "audio/pcmu", out_fmt: str = "audio/pcmu",
                 in_rate: int = None, out_rate: int = None):
        self.in_fmt = in_fmt
        self.out_fmt = out_fmt
        # g711 (pcmu/pcma) is always 8kHz; only linear pcm carries a negotiable rate.
        self.in_rate = int(in_rate) if in_rate else _WIRE_RATE.get(in_fmt, 8000)
        self.out_rate = int(out_rate) if out_rate else _WIRE_RATE.get(out_fmt, 8000)
        self._in_state = None  # ratecv state for input resample

    # ---- input: base64 wire audio -> PCM16 16kHz bytes ----
    def wire_to_pcm16_16k(self, b64_audio: str) -> bytes:
        if audioop is None:
            raise RuntimeError("audioop unavailable; install audioop-lts on Python 3.13+")
        raw = base64.b64decode(b64_audio)
        fmt = self.in_fmt
        if fmt == "audio/pcmu":
            pcm = audioop.ulaw2lin(raw, 2)      # -> PCM16 @ 8kHz
            src = 8000
        elif fmt == "audio/pcma":
            pcm = audioop.alaw2lin(raw, 2)      # -> PCM16 @ 8kHz
            src = 8000
        elif fmt == "audio/pcm":
            pcm = raw                           # PCM16 @ negotiated rate (default 24kHz)
            src = self.in_rate
        else:
            raise ValueError(f"unsupported input format: {fmt}")
        if src != NEMO_STT_RATE:
            pcm, self._in_state = audioop.ratecv(pcm, 2, 1, src, NEMO_STT_RATE, self._in_state)
        return pcm

    # ---- output: TTS WAV bytes -> list of base64 wire-audio chunks ----
    def wav_to_wire_chunks(self, wav_bytes: bytes, chunk_ms: int = 200):
        if audioop is None:
            raise RuntimeError("audioop unavailable; install audioop-lts on Python 3.13+")
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            nch = w.getnchannels()
            sw = w.getsampwidth()
            pcm = w.readframes(w.getnframes())
        if sw != 2:
            pcm = audioop.lin2lin(pcm, sw, 2)
        if nch == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)

        # g711 is fixed 8kHz; linear pcm uses the negotiated output rate.
        if self.out_fmt in ("audio/pcmu", "audio/pcma"):
            target = 8000
        elif self.out_fmt == "audio/pcm":
            target = self.out_rate
        else:
            raise ValueError(f"unsupported output format: {self.out_fmt}")
        if sr != target:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sr, target, None)

        if self.out_fmt == "audio/pcmu":
            wire = audioop.lin2ulaw(pcm, 2)
            bytes_per_sample = 1
        elif self.out_fmt == "audio/pcma":
            wire = audioop.lin2alaw(pcm, 2)
            bytes_per_sample = 1
        else:  # audio/pcm
            wire = pcm
            bytes_per_sample = 2

        step = max(1, int(target * chunk_ms / 1000) * bytes_per_sample)
        return [base64.b64encode(wire[i:i + step]).decode() for i in range(0, len(wire), step)]


# ---------------------------------------------------------------------------
# Per-connection session
# ---------------------------------------------------------------------------
class RealtimeSession:
    def __init__(self, ws, deps, settings: dict):
        self.ws = ws
        self.deps = deps
        self.id = _id("sess")

        # negotiated audio formats (default telephony)
        self.in_fmt = "audio/pcmu"
        self.out_fmt = "audio/pcmu"
        self.in_rate = self.out_rate = None   # only set for audio/pcm
        self.codec = AudioCodec(self.in_fmt, self.out_fmt)

        # generation config (seeded from server settings, overridable via session.update)
        self.system_prompt = settings.get("system_prompt")
        self.temperature = settings.get("temperature", 0.3)
        self.max_tokens = settings.get("max_tokens")
        self.speaker_id = settings.get("speaker", deps.default_speaker)
        self.speaker_en = settings.get("speaker_en", 0)

        self.history = []
        self.vad = deps.make_vad(
            threshold=settings.get("threshold", 0.5),
            silence_ms=settings.get("silence_ms", 600),
        )

        self.current_task = None      # asyncio.Task of the in-flight response
        self.interrupted = False      # barge-in / cancel flag
        self.speaking = False         # assistant currently emitting audio
        self.pending_text = None      # text from conversation.item.create
        self._vad_buf = b""           # accumulates PCM16 until a full VAD frame

    # ---- outbound event helper ----
    async def send(self, event: dict):
        event.setdefault("event_id", _id("event"))
        await self.ws.send_json(event)

    # ---- lifecycle ----
    async def start(self):
        await self.send({"type": "session.created", "session": self._session_obj()})
        await self.send({"type": "conversation.created",
                         "conversation": {"id": _id("conv")}})

    def _fmt_obj(self, fmt, rate):
        o = {"type": fmt}
        if fmt == "audio/pcm":
            o["rate"] = int(rate) if rate else _WIRE_RATE["audio/pcm"]
        return o

    def _session_obj(self):
        return {
            "id": self.id,
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self.system_prompt or "",
            "audio": {
                "input": {"format": self._fmt_obj(self.in_fmt, self.in_rate)},
                "output": {"format": self._fmt_obj(self.out_fmt, self.out_rate)},
            },
        }

    # ---- inbound event dispatch ----
    async def handle(self, ev: dict):
        t = ev.get("type")
        if t == "session.update":
            await self._on_session_update(ev)
        elif t == "input_audio_buffer.append":
            await self._on_audio_append(ev)
        elif t == "input_audio_buffer.commit":
            pass  # server-VAD drives turn end; manual commit is a no-op for now
        elif t == "input_audio_buffer.clear":
            pass
        elif t == "conversation.item.create":
            self._on_item_create(ev)
        elif t == "response.create":
            await self._on_response_create(ev)
        elif t == "response.cancel":
            await self._interrupt()
        # unknown types are ignored (OpenAI clients tolerate this)

    async def _on_session_update(self, ev):
        sess = ev.get("session", {}) or {}
        if sess.get("instructions"):
            self.system_prompt = sess["instructions"]
        if "temperature" in sess:
            try:
                self.temperature = float(sess["temperature"])
            except (TypeError, ValueError):
                pass
        audio = sess.get("audio", {}) or {}
        inp = (audio.get("input") or {}).get("format")
        out = (audio.get("output") or {}).get("format")
        in_rate = out_rate = None
        if isinstance(inp, dict) and inp.get("type"):
            self.in_fmt = inp["type"]
            in_rate = inp.get("rate")      # only meaningful for audio/pcm
        if isinstance(out, dict) and out.get("type"):
            self.out_fmt = out["type"]
            out_rate = out.get("rate")
        self.in_rate, self.out_rate = in_rate, out_rate
        self.codec = AudioCodec(self.in_fmt, self.out_fmt, in_rate, out_rate)
        await self.send({"type": "session.updated", "session": self._session_obj()})

    async def _on_audio_append(self, ev):
        b64 = ev.get("audio")
        if not b64 or self.vad is None:
            return
        try:
            pcm16 = self.codec.wire_to_pcm16_16k(b64)
        except Exception as e:  # bad audio / unsupported format
            await self.send({"type": "error",
                             "error": {"type": "invalid_request_error", "message": str(e)}})
            return
        # The VAD model needs fixed-size frames. Telephony clients stream tiny
        # per-packet chunks (~20ms = ~320 samples), so buffer and feed WHOLE
        # VAD_FRAME_BYTES frames only — otherwise the model raises "chunk too short".
        self._vad_buf += pcm16
        while len(self._vad_buf) >= VAD_FRAME_BYTES:
            frame = self._vad_buf[:VAD_FRAME_BYTES]
            self._vad_buf = self._vad_buf[VAD_FRAME_BYTES:]
            try:
                result = self.vad.process_chunk(frame)
            except Exception:
                continue  # a single bad frame must not drop the call
            if result:
                await self._handle_vad_result(result)

    async def _handle_vad_result(self, result):
        evt = result.get("event")
        if evt == "speech_start":
            # BARGE-IN: tell the client to flush playout, and cancel our in-flight response.
            await self.send({"type": "input_audio_buffer.speech_started"})
            await self._interrupt()
        elif evt == "speech_end":
            await self.send({"type": "input_audio_buffer.speech_stopped"})
            await self.send({"type": "input_audio_buffer.committed",
                             "item_id": _id("item")})
            if not self.deps.models_ready():
                await self.send({"type": "error",
                                 "error": {"type": "server_error",
                                           "message": "models still loading"}})
                return
            self._launch_response(audio_float32=result.get("audio"))
        # speech_too_short -> ignore

    def _on_item_create(self, ev):
        item = ev.get("item", {}) or {}
        text = None
        for part in item.get("content", []) or []:
            if part.get("type") in ("input_text", "text") and part.get("text"):
                text = part["text"]
                break
        if text:
            self.pending_text = text

    async def _on_response_create(self, ev):
        # explicit trigger (e.g. the initial greeting from a text item)
        text = self.pending_text
        self.pending_text = None
        self._launch_response(text_override=text)

    # ---- response generation ----
    def _launch_response(self, audio_float32=None, text_override=None):
        if self.current_task and not self.current_task.done():
            # a response is already running; ignore the new trigger
            return
        self.current_task = asyncio.create_task(
            self._generate(audio_float32=audio_float32, text_override=text_override)
        )

    async def _interrupt(self):
        if self.speaking:
            self.interrupted = True
        task = self.current_task
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _generate(self, audio_float32=None, text_override=None):
        self.interrupted = False
        self.speaking = True
        resp_id = _id("resp")
        try:
            await self.send({"type": "response.created",
                             "response": {"id": resp_id, "status": "in_progress"}})

            lang = "es"
            if text_override is not None:
                text = text_override
            else:
                result = await self.deps.transcribe(audio_float32)
                if isinstance(result, tuple):
                    text, lang = result
                else:
                    text = result
                if not text:
                    await self.send({"type": "response.done",
                                     "response": {"id": resp_id, "status": "cancelled"}})
                    return
                await self.send({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": _id("item"), "transcript": text,
                })

            if self.interrupted:
                await self.send({"type": "response.done",
                                 "response": {"id": resp_id, "status": "cancelled"}})
                return

            item_id = _id("item")
            await self.send({"type": "conversation.item.added",
                             "item": {"id": item_id, "role": "assistant", "type": "message"}})

            full = []
            async for sentence in self.deps.generate_sentences(
                text, self.system_prompt, self.temperature, self.history,
                max_tokens=self.max_tokens, lang=lang, append_user=True,
            ):
                if self.interrupted:
                    break
                full.append(sentence)
                await self.send({"type": "response.output_audio_transcript.delta",
                                 "item_id": item_id, "delta": sentence})
                sid = self.speaker_en if lang == "en" else self.speaker_id
                wav = await self.deps.synthesize(sentence, sid, lang)
                if self.interrupted:
                    break
                for chunk_b64 in self.codec.wav_to_wire_chunks(wav):
                    if self.interrupted:
                        break
                    await self.send({"type": "response.output_audio.delta",
                                     "item_id": item_id, "delta": chunk_b64})

            if self.interrupted:
                # Save the turn EVEN when barged-in, otherwise history stays empty and
                # the LLM keeps re-greeting with no conversational context.
                self._remember(text, full)
                await self.send({"type": "response.done",
                                 "response": {"id": resp_id, "status": "cancelled"}})
                return

            await self.send({"type": "response.output_audio_transcript.done",
                             "item_id": item_id, "transcript": " ".join(full)})
            await self.send({"type": "response.output_audio.done", "item_id": item_id})
            await self.send({"type": "conversation.item.done",
                             "item": {"id": item_id, "role": "assistant"}})
            await self.send({"type": "response.done",
                             "response": {"id": resp_id, "status": "completed"}})
            self._remember(text, full)
        except Exception as e:
            try:
                await self.send({"type": "error",
                                 "error": {"type": "server_error", "message": str(e)}})
            except Exception:
                pass
        finally:
            self.speaking = False

    def _remember(self, user_text, assistant_sentences):
        """Persist a conversation turn so context carries across turns — called on
        both completed and barged-in responses (empty history => the model re-greets)."""
        if user_text:
            self.history.append({"role": "user", "content": user_text})
        if assistant_sentences:
            self.history.append({"role": "assistant", "content": " ".join(assistant_sentences)})
        while len(self.history) > 30:
            self.history.pop(0)


# ---------------------------------------------------------------------------
# Endpoint registration
# ---------------------------------------------------------------------------
def register_realtime(app, deps):
    """Attach the /v1/realtime WebSocket endpoint to a FastAPI/Starlette `app`.

    `deps` must provide:
      - async check_key(token) -> bool
      - resolve_session(cookie) -> token|None     (optional; for browser cookies)
      - session_cookie: str                        (optional)
      - make_vad(threshold, silence_ms) -> vad|None
      - async transcribe(audio_float32) -> str | (str, lang)
      - generate_sentences(text, system_prompt, temperature, history, *, max_tokens, lang, append_user) -> async-gen
      - async synthesize(text, speaker_id, lang) -> wav_bytes
      - async get_settings() -> dict
      - models_ready() -> bool
      - default_speaker: int
    """

    @app.websocket("/v1/realtime")
    async def realtime_endpoint(ws: WebSocket):
        # Auth, in order: Bearer header · X-Api-Key · OpenAI browser subprotocol
        # (`openai-insecure-api-key.<KEY>`, since browser WS can't set headers) · cookie.
        subprotos = ws.scope.get("subprotocols", []) or []
        token = (ws.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not token:
            token = ws.headers.get("x-api-key", "")
        if not token:
            for sp in subprotos:
                if sp.startswith("openai-insecure-api-key."):
                    token = sp.split(".", 1)[1]
                    break
        if not token and getattr(deps, "resolve_session", None) and getattr(deps, "session_cookie", None):
            token = deps.resolve_session(ws.cookies.get(deps.session_cookie)) or ""
        # Echo the "realtime" subprotocol when the client offered it (browser clients require it).
        accept_kwargs = {"subprotocol": "realtime"} if "realtime" in subprotos else {}
        if not await deps.check_key(token):
            await ws.accept(**accept_kwargs)
            await ws.close(code=4001, reason="unauthorized")
            return

        await ws.accept(**accept_kwargs)
        try:
            settings = await deps.get_settings()
        except Exception:
            settings = {}

        session = RealtimeSession(ws, deps, settings)
        await session.start()

        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if not text:
                    # OpenAI protocol carries audio as base64 inside JSON, so binary
                    # frames aren't expected; ignore them.
                    continue
                try:
                    ev = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    continue
                await session.handle(ev)
        except Exception as e:
            print(f"[realtime] connection error: {e}")
        finally:
            await session._interrupt()

    return realtime_endpoint
