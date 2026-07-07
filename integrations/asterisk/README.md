# Asterisk / SIP → Nemo-RT

Answer real phone calls with your **on-prem** voice AI. This bridge connects an
**Asterisk PBX** (via ARI + external-media RTP) to a **Nemo-RT** box speaking the
OpenAI Realtime protocol — a caller dials in, and Nemo-RT transcribes, thinks and
speaks back, **on your own GPU**. No per-minute fees, no audio leaving your network.

Because Nemo-RT is a drop-in for the OpenAI Realtime API, this same bridge also
talks to OpenAI's cloud **unchanged** — only `REALTIME_URL` changes.

```diff
- REALTIME_URL=wss://api.openai.com/v1/realtime?model=gpt-realtime   # cloud, metered
+ REALTIME_URL=ws://localhost:8000/v1/realtime                        # your box, free
```

> ✅ **Validated end-to-end:** a live SIP call (PJSIP) answered, transcribed and
> spoken back in Spanish on a single H100 running Nemo-RT — greeting, multi-turn
> Q&A and **barge-in** (interrupt mid-sentence), clean teardown. Same bridge,
> same code, against OpenAI or against Nemo-RT — only the URL differs.

---

## Requirements

- **Node.js 18+**
- An **Asterisk** server with **ARI enabled** and a dialplan that routes calls into a
  Stasis app named `asterisk_to_openai_rt` (see below).
- A running **Nemo-RT** box (`../../setup.sh` in this repo) — or an OpenAI API key.

## Quick start

```bash
cd integrations/asterisk
npm install
cp config.conf.example config.conf
# edit config.conf → REALTIME_URL, OPENAI_API_KEY, ARI creds
node index.js
```

On start you should see `Connected to ARI` and `ARI application "asterisk_to_openai_rt" started`.
Dial the extension routed to Stasis and talk.

### Point it at Nemo-RT

- **Same box as Nemo-RT:** `REALTIME_URL=ws://localhost:8000/v1/realtime` (default).
- **Different host:** `ws://<nemo-box-ip>:8000/v1/realtime`, or forward the port over
  SSH and keep `localhost` — no public URL, **no HTTPS/Cloudflare needed** (that
  tunnel is only for the browser mic; a SIP call never touches it):
  ```bash
  ssh -L 8000:localhost:8000 user@<nemo-box>
  ```
- `OPENAI_API_KEY` = the key `setup.sh` printed on your Nemo-RT box.

## Asterisk dialplan

Route the calls you want handled into the Stasis app:

```ini
; extensions.conf
[from-internal]
exten => _.,1,NoOp(-> Nemo-RT voice agent)
 same => n,Stasis(asterisk_to_openai_rt)
 same => n,Hangup()
```

Enable ARI in `ari.conf` / `http.conf` and set the same user/password in `config.conf`.

## How it works

```
caller ──SIP──> Asterisk ──ARI external-media (RTP, g711 μ-law)──> this bridge
                                                                       │
                                              WebSocket /v1/realtime (GA protocol)
                                                                       ▼
                                                                   Nemo-RT
                              VAD → STT → LLM → TTS  (all on your GPU)
```

The bridge speaks the **OpenAI Realtime GA** event shape: `session.update` with
`audio.input/output.format = audio/pcmu` (telephony g711), server-VAD turn
detection, streams `response.output_audio.delta` back as RTP, and does **barge-in**
via `input_audio_buffer.speech_started` → `response.cancel` (stop playback + cancel
the in-flight response the instant the caller starts talking).

## Notes

- **Telephony audio is g711 μ-law 8kHz** (`audio/pcmu`) — the bridge negotiates that
  format; Nemo-RT resamples internally.
- `TRANSCRIPTION_LANGUAGE` only affects the *displayed* caller transcript (Whisper);
  the model understands the audio natively regardless. `es` / `en` / empty=auto.
- `config.conf` is gitignored — keep your keys out of git.

## License

Apache 2.0 — © INFINITO CLOUD LLC. Part of
[nemo-rt-community](../../). Built by [INFINITO CLOUD](https://infinitocloud.com).
