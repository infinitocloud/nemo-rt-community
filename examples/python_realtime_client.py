#!/usr/bin/env python3
"""
Minimal OpenAI Realtime API client — works against Nemo-RT or OpenAI *unchanged*.

Sends a text prompt, streams the spoken reply, writes it to reply.wav.
The ONLY thing that changes between your own box and OpenAI is REALTIME_URL + API_KEY.

    pip install websockets

    # Against your own Nemo-RT box (on-prem, free):
    REALTIME_URL="ws://localhost:8000/v1/realtime" API_KEY="sk-..." \
        python python_realtime_client.py "Hola, ¿qué servicios ofrecen?"

    # Against OpenAI (drop-in — same script):
    REALTIME_URL="wss://api.openai.com/v1/realtime?model=gpt-realtime" API_KEY="sk-openai..." \
        python python_realtime_client.py "Hello, what do you offer?"
"""
import asyncio
import base64
import json
import os
import sys
import wave

import websockets  # pip install websockets

URL = os.environ.get("REALTIME_URL", "ws://localhost:8000/v1/realtime")
KEY = os.environ.get("API_KEY", "")
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "Hola, ¿en qué me pueden ayudar?"
RATE = 24000  # audio/pcm sample rate (linear PCM16)


async def main():
    headers = {"Authorization": f"Bearer {KEY}"}
    # websockets >=13 uses additional_headers; older uses extra_headers.
    try:
        ws = await websockets.connect(URL, additional_headers=headers)
    except TypeError:
        ws = await websockets.connect(URL, extra_headers=headers)

    async with ws:
        # 1) Configure the session — audio out as PCM16 @ 24kHz (OpenAI's default).
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}},
            },
        }}))

        # 2) Send a text message and ask for a spoken response.
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": PROMPT}],
        }}))
        await ws.send(json.dumps({"type": "response.create"}))

        # 3) Collect the streamed audio + transcript.
        pcm = bytearray()
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            if t == "response.output_audio.delta":
                pcm += base64.b64decode(m["delta"])
            elif t == "response.output_audio_transcript.done":
                print("Assistant:", m.get("transcript", ""))
            elif t == "response.done":
                break
            elif t == "error":
                print("ERROR:", m.get("error"))
                break

    with wave.open("reply.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))
    print(f"Wrote reply.wav ({len(pcm)} bytes, {len(pcm) / 2 / RATE:.1f}s of audio)")


if __name__ == "__main__":
    asyncio.run(main())
