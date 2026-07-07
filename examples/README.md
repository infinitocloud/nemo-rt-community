# Talk to Nemo-RT like the OpenAI Realtime API

Nemo-RT Community exposes a **`/v1/realtime` WebSocket endpoint that speaks the
OpenAI Realtime protocol**. Any OpenAI Realtime client, SDK or bridge works against
your own box **unchanged** — a drop-in, on-prem replacement for the cloud API.

The only difference between talking to OpenAI and talking to your own GPU is **one line**:

```diff
- REALTIME_URL="wss://api.openai.com/v1/realtime?model=gpt-realtime"   # OpenAI cloud, metered
+ REALTIME_URL="ws://YOUR-BOX:8000/v1/realtime"                        # your GPU, on-prem, free
  Authorization: Bearer <API_KEY>
```

## Protocol (same events as OpenAI Realtime GA)

**You send:** `session.update` · `input_audio_buffer.append` · `input_audio_buffer.commit` ·
`conversation.item.create` · `response.create` · `response.cancel`

**You receive:** `session.created` / `session.updated` · `input_audio_buffer.speech_started` /
`speech_stopped` · `response.created` · `response.output_audio.delta` (base64 audio) ·
`response.output_audio_transcript.delta` / `done` · `response.output_audio.done` · `response.done` · `error`

**Audio formats:** `audio/pcm` (linear PCM16, default 24 kHz — for apps/browsers) ·
`audio/pcmu` (g711 μ-law 8 kHz — telephony/SIP) · `audio/pcma`.

## Examples in this folder

| File | What it does |
|------|--------------|
| [`python_realtime_client.py`](python_realtime_client.py) | Send a text prompt → stream the spoken reply → save `reply.wav`. `pip install websockets` |
| [`node_realtime_client.js`](node_realtime_client.js) | Same, in Node. `npm i ws` |

For a **live browser client** (mic in / audio out, real-time, barge-in), the bundled web
UI runs on this endpoint too: open it and pick **Settings → API → OpenAI Realtime (`/v1/realtime`)**.

### Quick start

```bash
# Python
pip install websockets
REALTIME_URL="ws://localhost:8000/v1/realtime" API_KEY="sk-..." \
  python python_realtime_client.py "Hola, ¿qué servicios ofrecen?"

# Node
npm i ws
REALTIME_URL="ws://localhost:8000/v1/realtime" API_KEY="sk-..." \
  node node_realtime_client.js "Hola, ¿qué servicios ofrecen?"
```

Point the same command at OpenAI (`wss://api.openai.com/v1/realtime?model=gpt-realtime` + your
OpenAI key) and it behaves the same — that's the whole idea.

## Auth

- **Server-side (recommended):** `Authorization: Bearer <API_KEY>` header (these examples).
- **Browser:** the WebSocket API can't set headers, so pass the key as a subprotocol —
  `["realtime", "openai-insecure-api-key.<API_KEY>"]`. Same-origin cookie auth also works
  when serving the bundled UI.

> Telephony? Point any existing **Asterisk → OpenAI Realtime** bridge at
> `ws://YOUR-BOX:8000/v1/realtime` (format `audio/pcmu`) and your whole phone stack runs on-prem.

Full protocol reference: <https://platform.openai.com/docs/guides/realtime>
