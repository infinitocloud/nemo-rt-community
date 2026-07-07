#!/usr/bin/env node
/*
 * Minimal OpenAI Realtime API client — works against Nemo-RT or OpenAI *unchanged*.
 *
 * Sends a text prompt, streams the spoken reply, writes it to reply.wav.
 * The ONLY thing that changes between your own box and OpenAI is REALTIME_URL + API_KEY.
 *
 *   npm i ws
 *
 *   # Against your own Nemo-RT box (on-prem, free):
 *   REALTIME_URL="ws://localhost:8000/v1/realtime" API_KEY="sk-..." \
 *       node node_realtime_client.js "Hola, ¿qué servicios ofrecen?"
 *
 *   # Against OpenAI (drop-in — same script):
 *   REALTIME_URL="wss://api.openai.com/v1/realtime?model=gpt-realtime" API_KEY="sk-openai..." \
 *       node node_realtime_client.js "Hello, what do you offer?"
 */
const fs = require('fs');
const WebSocket = require('ws'); // npm i ws

const URL = process.env.REALTIME_URL || 'ws://localhost:8000/v1/realtime';
const KEY = process.env.API_KEY || '';
const PROMPT = process.argv[2] || 'Hola, ¿en qué me pueden ayudar?';
const RATE = 24000; // audio/pcm sample rate (linear PCM16)

const ws = new WebSocket(URL, { headers: { Authorization: `Bearer ${KEY}` } });
let pcm = Buffer.alloc(0);

ws.on('open', () => {
  // 1) Configure the session — audio out as PCM16 @ 24kHz (OpenAI's default).
  ws.send(JSON.stringify({ type: 'session.update', session: {
    type: 'realtime', output_modalities: ['audio'],
    audio: { input: { format: { type: 'audio/pcm', rate: RATE } },
             output: { format: { type: 'audio/pcm', rate: RATE } } },
  } }));
  // 2) Send a text message and ask for a spoken response.
  ws.send(JSON.stringify({ type: 'conversation.item.create', item: {
    type: 'message', role: 'user', content: [{ type: 'input_text', text: PROMPT }] } }));
  ws.send(JSON.stringify({ type: 'response.create' }));
});

ws.on('message', (data) => {
  const m = JSON.parse(data);
  switch (m.type) {
    case 'response.output_audio.delta':
      pcm = Buffer.concat([pcm, Buffer.from(m.delta, 'base64')]);
      break;
    case 'response.output_audio_transcript.done':
      console.log('Assistant:', m.transcript || '');
      break;
    case 'response.done':
      writeWav(); ws.close(); break;
    case 'error':
      console.error('ERROR', m.error); ws.close(); break;
  }
});
ws.on('error', (e) => console.error('WS error:', e.message));

function writeWav() {
  const h = Buffer.alloc(44);
  h.write('RIFF', 0); h.writeUInt32LE(36 + pcm.length, 4); h.write('WAVE', 8);
  h.write('fmt ', 12); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(1, 22);
  h.writeUInt32LE(RATE, 24); h.writeUInt32LE(RATE * 2, 28); h.writeUInt16LE(2, 32); h.writeUInt16LE(16, 34);
  h.write('data', 36); h.writeUInt32LE(pcm.length, 40);
  fs.writeFileSync('reply.wav', Buffer.concat([h, pcm]));
  console.log(`Wrote reply.wav (${pcm.length} bytes, ${(pcm.length / 2 / RATE).toFixed(1)}s of audio)`);
}
