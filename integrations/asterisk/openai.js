const WebSocket = require('ws');
const { v4: uuid } = require('uuid');
const { config, logger, logClient, logOpenAI } = require('./config');
const { sipMap, cleanupPromises } = require('./state');
const { streamAudio, rtpEvents } = require('./rtp');

logger.info('Loading openai.js module');

async function waitForBufferEmpty(channelId, maxWaitTime = 6000, checkInterval = 10) {
  const channelData = sipMap.get(channelId);
  if (!channelData?.streamHandler) {
    logOpenAI(`No streamHandler for ${channelId}, proceeding`, 'info');
    return true;
  }
  const streamHandler = channelData.streamHandler;
  const startWaitTime = Date.now();

  let audioDurationMs = 1000; // Default minimum
  if (channelData.totalDeltaBytes) {
    audioDurationMs = Math.ceil((channelData.totalDeltaBytes / 8000) * 1000) + 500; // Audio duration + 500ms margin
  }
  const dynamicTimeout = Math.min(audioDurationMs, maxWaitTime);
  logOpenAI(`Using dynamic timeout of ${dynamicTimeout}ms for ${channelId} (estimated audio duration: ${(channelData.totalDeltaBytes || 0) / 8000}s)`, 'info');

  let audioFinishedReceived = false;
  const audioFinishedPromise = new Promise((resolve) => {
    rtpEvents.once('audioFinished', (id) => {
      if (id === channelId) {
        logOpenAI(`Audio finished sending for ${channelId} after ${Date.now() - startWaitTime}ms`, 'info');
        audioFinishedReceived = true;
        resolve();
      }
    });
  });

  const isBufferEmpty = () => (
    (!streamHandler.audioBuffer || streamHandler.audioBuffer.length === 0) &&
    (!streamHandler.packetQueue || streamHandler.packetQueue.length === 0)
  );
  if (!isBufferEmpty()) {
    let lastLogTime = 0;
    while (!isBufferEmpty() && (Date.now() - startWaitTime) < maxWaitTime) {
      const now = Date.now();
      if (now - lastLogTime >= 50) {
        logOpenAI(`Waiting for RTP buffer to empty for ${channelId} | Buffer: ${streamHandler.audioBuffer?.length || 0} bytes, Queue: ${streamHandler.packetQueue?.length || 0} packets`, 'info');
        lastLogTime = now;
      }
      await new Promise(resolve => setTimeout(resolve, checkInterval));
    }
    if (!isBufferEmpty()) {
      logger.warn(`Timeout waiting for RTP buffer to empty for ${channelId} after ${maxWaitTime}ms`);
      return false;
    }
    logOpenAI(`RTP buffer emptied for ${channelId} after ${Date.now() - startWaitTime}ms`, 'info');
  }

  const timeoutPromise = new Promise((resolve) => {
    setTimeout(() => {
      if (!audioFinishedReceived) {
        logger.warn(`Timeout waiting for audioFinished for ${channelId} after ${dynamicTimeout}ms`);
      }
      resolve();
    }, dynamicTimeout);
  });
  await Promise.race([audioFinishedPromise, timeoutPromise]);

  logOpenAI(`waitForBufferEmpty completed for ${channelId} in ${Date.now() - startWaitTime}ms`, 'info');
  return true;
}

async function startOpenAIWebSocket(channelId) {
  const OPENAI_API_KEY = config.OPENAI_API_KEY;
  if (!OPENAI_API_KEY) {
    logger.error('OPENAI_API_KEY is missing in config');
    throw new Error('Missing OPENAI_API_KEY');
  }

  let channelData = sipMap.get(channelId);
  if (!channelData) {
    throw new Error(`Channel ${channelId} not found in sipMap`);
  }

  let ws;
  let streamHandler = null;
  let retryCount = 0;
  const maxRetries = 3;
  let isResponseActive = false;
  let totalDeltaBytes = 0;
  let loggedDeltaBytes = 0;
  let segmentCount = 0;
  let responseBuffer = Buffer.alloc(0);
  let messageQueue = [];
  let itemRoles = new Map();
  let lastUserItemId = null;

  const processMessage = async (response) => {
    try {
      switch (response.type) {
        case 'session.created':
          logClient(`Session created for ${channelId}`);
          break;
        case 'session.updated':
          logOpenAI(`Session updated for ${channelId}`);
          break;
        case 'conversation.item.added':
        case 'conversation.item.done':
          logOpenAI(`Conversation item ${response.type.split('.').pop()} for ${channelId}`);
          if (response.item && response.item.id && response.item.role) {
            itemRoles.set(response.item.id, response.item.role);
            if (response.item.role === 'user') lastUserItemId = response.item.id;
          }
          break;
        case 'input_audio_buffer.speech_started':
          // GA barge-in: user started speaking mid-response → flush playout + cancel.
          logOpenAI(`User speech started for ${channelId}, barge-in: stopping playback + cancelling response`);
          if (streamHandler) {
            streamHandler.stopPlayback();
          }
          if (isResponseActive && ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'response.cancel' }));
          }
          break;
        case 'response.created':
          logOpenAI(`Response created for ${channelId}`);
          break;
        case 'response.output_audio.delta':
          if (response.delta) {
            const deltaBuffer = Buffer.from(response.delta, 'base64');
            if (deltaBuffer.length > 0 && !deltaBuffer.every(byte => byte === 0x7F)) {
              totalDeltaBytes += deltaBuffer.length;
              channelData.totalDeltaBytes = totalDeltaBytes; // Store in channelData
              sipMap.set(channelId, channelData);
              segmentCount++;
              if (totalDeltaBytes - loggedDeltaBytes >= 40000 || segmentCount >= 100) {
                logOpenAI(`Received audio delta for ${channelId}: ${deltaBuffer.length} bytes, total: ${totalDeltaBytes} bytes, estimated duration: ${(totalDeltaBytes / 8000).toFixed(2)}s`, 'info');
                loggedDeltaBytes = totalDeltaBytes;
                segmentCount = 0;
              }

              let packetBuffer = deltaBuffer;
              if (totalDeltaBytes === deltaBuffer.length) {
                const silenceDurationMs = config.SILENCE_PADDING_MS || 100;
                const silencePackets = Math.ceil(silenceDurationMs / 20);
                const silenceBuffer = Buffer.alloc(silencePackets * 160, 0x7F);
                packetBuffer = Buffer.concat([silenceBuffer, deltaBuffer]);
                logger.info(`Prepended ${silencePackets} silence packets (${silenceDurationMs} ms) for ${channelId}`);
              }

              if (sipMap.has(channelId) && streamHandler) {
                streamHandler.sendRtpPacket(packetBuffer);
              }
            } else {
              logger.warn(`Received empty or silent delta for ${channelId}`);
            }
          }
          break;
        case 'response.output_audio_transcript.delta':
          if (response.delta) {
            logger.debug(`Transcript delta for ${channelId}: ${response.delta.trim()}`);
            logger.debug(`Full transcript delta message: ${JSON.stringify(response, null, 2)}`);
          }
          break;
        case 'response.output_audio_transcript.done':
          if (response.transcript) {
            const role = response.item_id && itemRoles.get(response.item_id) ? itemRoles.get(response.item_id) : (lastUserItemId ? 'User' : 'Assistant');
            logger.debug(`Transcript done - Full message: ${JSON.stringify(response, null, 2)}`);
            if (role === 'User') {
              logOpenAI(`User command transcription for ${channelId}: ${response.transcript}`, 'info');
            } else {
              logOpenAI(`Assistant transcription for ${channelId}: ${response.transcript}`, 'info');
            }
          }
          break;
        case 'conversation.item.input_audio_transcription.delta':
          if (response.delta) {
            logger.debug(`User transcript delta for ${channelId}: ${response.delta.trim()}`);
            logger.debug(`Full user transcript delta message: ${JSON.stringify(response, null, 2)}`);
          }
          break;
        case 'conversation.item.input_audio_transcription.completed':
          if (response.transcript) {
            logger.debug(`User transcript completed - Full message: ${JSON.stringify(response, null, 2)}`);
            logOpenAI(`User command transcription for ${channelId}: ${response.transcript}`, 'info');
          }
          break;
        case 'response.output_audio.done':
          logOpenAI(`Response audio done for ${channelId}, total delta bytes: ${totalDeltaBytes}, estimated duration: ${(totalDeltaBytes / 8000).toFixed(2)}s`, 'info');
          isResponseActive = false;
          loggedDeltaBytes = 0;
          segmentCount = 0;
          itemRoles.clear();
          lastUserItemId = null;
          responseBuffer = Buffer.alloc(0);
          break;
        case 'error':
          logger.error(`OpenAI error for ${channelId}: ${response.error.message}`);
          ws.close();
          break;
        default:
          logger.debug(`Unhandled event type: ${response.type} for ${channelId}`);
          break;
      }
    } catch (e) {
      logger.error(`Error processing message for ${channelId}: ${e.message}`);
    }
  };

  const connectWebSocket = () => {
    return new Promise((resolve, reject) => {
      ws = new WebSocket(config.REALTIME_URL, {
        headers: {
          'Authorization': `Bearer ${OPENAI_API_KEY}`
        }
      });

      ws.on('open', async () => {
        logClient(`OpenAI WebSocket connected for ${channelId}`);
        ws.send(JSON.stringify({
          type: 'session.update',
          session: {
            type: 'realtime',
            output_modalities: ['audio'],
            instructions: config.SYSTEM_PROMPT,
            audio: {
              input: {
                format: { type: 'audio/pcmu' },
                turn_detection: {
                  type: 'server_vad',
                  threshold: config.VAD_THRESHOLD || 0.6,
                  prefix_padding_ms: config.VAD_PREFIX_PADDING_MS || 200,
                  silence_duration_ms: config.VAD_SILENCE_DURATION_MS || 600
                },
                transcription: { model: 'whisper-1', language: config.TRANSCRIPTION_LANGUAGE }
              },
              output: {
                format: { type: 'audio/pcmu' },
                voice: config.OPENAI_VOICE || 'alloy'
              }
            }
          }
        }));
        logClient(`Session updated for ${channelId}`);

        try {
          const rtpSource = channelData.rtpSource || { address: '127.0.0.1', port: 12000 };
          streamHandler = await streamAudio(channelId, rtpSource);
          channelData.ws = ws;
          channelData.streamHandler = streamHandler;
          channelData.totalDeltaBytes = 0; // Initialize totalDeltaBytes
          sipMap.set(channelId, channelData);

          const itemId = uuid().replace(/-/g, '').substring(0, 32);
          logClient(`Sending initial message for ${channelId}: ${config.INITIAL_MESSAGE || 'Hi'}`);
          ws.send(JSON.stringify({
            type: 'conversation.item.create',
            item: {
              id: itemId,
              type: 'message',
              role: 'user',
              content: [{ type: 'input_text', text: config.INITIAL_MESSAGE || 'Hi' }]
            }
          }));
          ws.send(JSON.stringify({
            type: 'response.create',
            response: {
              output_modalities: ['audio'],
              instructions: config.SYSTEM_PROMPT
            }
          }));
          logClient(`Requested response for ${channelId}`);
          isResponseActive = true;
          resolve(ws);
        } catch (e) {
          logger.error(`Error setting up WebSocket for ${channelId}: ${e.message}`);
          reject(e);
        }
      });

      ws.on('message', (data) => {
        try {
          const response = JSON.parse(data.toString());
          logger.debug(`Raw WebSocket message for ${channelId}: ${JSON.stringify(response, null, 2)}`);
          messageQueue.push(response);
        } catch (e) {
          logger.error(`Error parsing WebSocket message for ${channelId}: ${e.message}`);
        }
      });

      ws.on('error', (e) => {
        logger.error(`WebSocket error for ${channelId}: ${e.message}`);
        if (retryCount < maxRetries && sipMap.has(channelId)) {
          retryCount++;
          setTimeout(() => connectWebSocket().then(resolve).catch(reject), 1000);
        } else {
          reject(new Error(`Failed WebSocket after ${maxRetries} attempts`));
        }
      });

      const handleClose = () => {
        logger.info(`WebSocket closed for ${channelId}`);
        channelData.wsClosed = true;
        channelData.ws = null;
        sipMap.set(channelId, channelData);
        ws.off('close', handleClose);
        const cleanupResolve = cleanupPromises.get(`ws_${channelId}`);
        if (cleanupResolve) {
          cleanupResolve();
          cleanupPromises.delete(`ws_${channelId}`);
        }
      };
      ws.on('close', handleClose);
    });
  };

  setInterval(async () => {
    const maxMessages = 5;
    for (let i = 0; i < maxMessages && messageQueue.length > 0; i++) {
      await processMessage(messageQueue.shift());
    }
  }, 25);

  try {
    await connectWebSocket();
  } catch (e) {
    logger.error(`Failed to start WebSocket for ${channelId}: ${e.message}`);
    throw e;
  }
}

module.exports = { startOpenAIWebSocket };
