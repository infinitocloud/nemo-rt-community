#!/usr/bin/env python3
"""
GPU-free protocol-conformance test for the /v1/realtime endpoint.

Proves the core launch claim ("drop-in replacement for the OpenAI Realtime API")
at the PROTOCOL level: with the model pipeline (VAD/STT/LLM/TTS) mocked, the
RealtimeSession must emit the exact OpenAI GA event contract and honor barge-in
(response.cancel). No GPU, no model weights — pure event-shape verification.

Run:  python tests/test_realtime_contract.py
Exit 0 = all contracts hold.
"""
import asyncio
import base64
import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from realtime_api import RealtimeSession  # noqa: E402


# ---- a WAV the mocked TTS returns (200ms PCM16 mono @ 16kHz, silence) --------
def _fake_wav(ms=200, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


# ---- fakes -------------------------------------------------------------------
class FakeWS:
    """Captures every event the server emits (what a real OpenAI client sees)."""
    def __init__(self):
        self.events = []
    async def send_json(self, event):
        self.events.append(event)


class FakeDeps:
    default_speaker = 0
    def __init__(self, sentences=("Hola.", "¿En qué puedo ayudarte?"), synth_delay=0.0):
        self._sentences = sentences
        self._synth_delay = synth_delay
    async def check_key(self, token):
        return True
    def make_vad(self, threshold=0.5, silence_ms=600):
        return object()  # unused on the text path
    async def transcribe(self, audio_float32):
        return "texto de prueba", "es"
    async def generate_sentences(self, text, system_prompt, temperature, history,
                                 *, max_tokens=None, lang="es", append_user=True):
        for s in self._sentences:
            if self._synth_delay:
                await asyncio.sleep(self._synth_delay)
            yield s
    async def synthesize(self, text, speaker_id, lang):
        return _fake_wav()
    async def get_settings(self):
        return {}
    def models_ready(self):
        return True


# ---- helpers -----------------------------------------------------------------
def types_of(events):
    return [e["type"] for e in events]


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---- Test 1: text-driven happy path emits the full GA contract ---------------
async def test_text_path_contract():
    ws, deps = FakeWS(), FakeDeps()
    s = RealtimeSession(ws, deps, settings={"system_prompt": "Eres un asistente."})
    await s.start()
    await s.handle({"type": "session.update", "session": {
        "type": "realtime", "output_modalities": ["audio"],
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}},
                  "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}})
    await s.handle({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "Hola"}]}})
    await s.handle({"type": "response.create"})
    await s.current_task  # let _generate finish

    ts = types_of(ws.events)
    # required GA events, in this relative order
    required = [
        "session.created", "conversation.created", "session.updated",
        "response.created", "conversation.item.added",
        "response.output_audio_transcript.delta", "response.output_audio.delta",
        "response.output_audio_transcript.done", "response.output_audio.done",
        "conversation.item.done", "response.done",
    ]
    last = -1
    for r in required:
        assert_(r in ts, f"missing GA event: {r}  (got: {ts})")
        idx = ts.index(r, last + 1)
        assert_(idx > last, f"event out of order: {r}  (got: {ts})")
        last = idx

    # every event carries an event_id (OpenAI clients rely on it)
    assert_(all("event_id" in e for e in ws.events), "some events lack event_id")

    # audio deltas must be valid base64 and non-empty
    deltas = [e for e in ws.events if e["type"] == "response.output_audio.delta"]
    assert_(deltas, "no audio deltas emitted")
    for d in deltas:
        raw = base64.b64decode(d["delta"])  # raises if not valid b64
        assert_(len(raw) > 0, "empty audio delta")

    # session.updated must reflect the negotiated pcm/24k format
    su = next(e for e in ws.events if e["type"] == "session.updated")
    fmt = su["session"]["audio"]["output"]["format"]
    assert_(fmt == {"type": "audio/pcm", "rate": 24000}, f"bad negotiated format: {fmt}")

    # response.done must report completed
    done = next(e for e in ws.events if e["type"] == "response.done")
    assert_(done["response"]["status"] == "completed",
            f"response.done not completed: {done}")
    return "text-path GA contract"


# ---- Test 2: response.cancel (barge-in) is honored ---------------------------
async def test_barge_in_cancel():
    ws = FakeWS()
    # many slow sentences so we can cancel mid-stream
    deps = FakeDeps(sentences=tuple(f"frase {i}" for i in range(20)), synth_delay=0.02)
    s = RealtimeSession(ws, deps, settings={})
    await s.start()
    await s.handle({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "cuéntame algo largo"}]}})
    await s.handle({"type": "response.create"})

    # wait until it is actually speaking, then cancel
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.speaking and any(e["type"] == "response.output_audio.delta" for e in ws.events):
            break
    await s.handle({"type": "response.cancel"})
    if s.current_task:
        await s.current_task

    done = next(e for e in ws.events if e["type"] == "response.done")
    assert_(done["response"]["status"] == "cancelled",
            f"cancel did not produce cancelled status: {done}")
    # it must have stopped EARLY (not all 20 sentences streamed)
    n_sent = sum(1 for e in ws.events if e["type"] == "response.output_audio_transcript.delta")
    assert_(n_sent < 20, f"did not stop early on cancel: streamed {n_sent}/20")
    # history must be remembered even on barge-in (else the LLM re-greets forever)
    assert_(len(s.history) >= 1, "history empty after barge-in (re-greet bug)")
    return f"barge-in cancel (stopped at {n_sent}/20, history kept)"


# ---- Test 3: unknown client events are tolerated (OpenAI clients send extras)-
async def test_unknown_events_tolerated():
    ws, deps = FakeWS(), FakeDeps()
    s = RealtimeSession(ws, deps, settings={})
    await s.start()
    await s.handle({"type": "input_audio_buffer.commit"})   # no-op
    await s.handle({"type": "some.future.event", "foo": 1})  # ignored
    assert_(not any(e["type"] == "error" for e in ws.events),
            "unknown events wrongly produced an error")
    return "unknown-event tolerance"


async def main():
    tests = [test_text_path_contract, test_barge_in_cancel, test_unknown_events_tolerated]
    failed = 0
    for t in tests:
        try:
            label = await t()
            print(f"  PASS  {label}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print()
    if failed:
        print(f"{failed}/{len(tests)} contract test(s) FAILED")
        return 1
    print(f"all {len(tests)} /v1/realtime contract tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
