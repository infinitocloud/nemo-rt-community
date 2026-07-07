const dgram = require('dgram');
const { EventEmitter } = require('events');
const { config, logger } = require('./config');
const { sipMap, rtpSenders, rtpReceivers } = require('./state');

logger.info('Loading rtp.js module');

const usedRtpPorts = new Set();
const rtpEvents = new EventEmitter();

function getNextRtpPort() {
  let port = config.RTP_PORT_START;
  while (usedRtpPorts.has(port)) port += 2;
  if (usedRtpPorts.size >= config.MAX_CONCURRENT_CALLS) {
    logger.warn('Maximum concurrent calls reached, reusing oldest port');
    const oldestPort = Math.min(...usedRtpPorts);
    usedRtpPorts.delete(oldestPort);
    return oldestPort;
  }
  usedRtpPorts.add(port);
  return port;
}

function releaseRtpPort(port) {
  usedRtpPorts.delete(port);
}

function startRTPReceiver(channelId, port) {
  const rtpReceiver = dgram.createSocket('udp4');
  rtpReceiver.isOpen = true;
  rtpReceivers.set(channelId, rtpReceiver);

  rtpReceiver.on('listening', () => logger.info(`RTP Receiver for ${channelId} listening on 127.0.0.1:${port}`));
  rtpReceiver.on('message', (msg, rinfo) => {
    const channelData = sipMap.get(channelId);
    if (channelData && !channelData.rtpSource) {
      channelData.rtpSource = { address: rinfo.address, port: rinfo.port };
      sipMap.set(channelId, channelData);
      logger.info(`RTP source assigned for ${channelId}: ${rinfo.address}:${rinfo.port}`);
    }
    if (channelData && channelData.ws && channelData.ws.readyState === 1) {
      const muLawData = msg.slice(12);
      channelData.ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: muLawData.toString('base64') }));
    }
  });
  rtpReceiver.on('error', (err) => logger.error(`RTP Receiver error for ${channelId}: ${err.message}`));
  rtpReceiver.bind(port, '127.0.0.1');
}

function buildRTPHeader(seq, timestamp, ssrc) {
  const header = Buffer.alloc(12);
  header[0] = 0x80;
  header[1] = 0x00;
  header.writeUInt16BE(seq, 2);
  header.writeUInt32BE(timestamp, 4);
  header.writeUInt32BE(ssrc, 8);
  return header;
}

async function streamAudio(channelId, rtpSource) {
  logger.info(`Initializing RTP stream to ${rtpSource.address}:${rtpSource.port} for ${channelId}`);
  let audioBuffer = Buffer.alloc(0);
  let rtpSequence = Math.floor(Math.random() * 65535);
  let rtpTimestamp = 0;
  const rtpSsrc = Math.floor(Math.random() * 4294967295);
  let totalPacketsSent = 0;
  const maxBufferSize = 640;
  const samplesPerPacket = 160;
  let lastBufferWarnTime = 0;
  let totalBytesSent = 0;
  let isSocketClosed = false;
  const ptimeStats = { count: 0, sum: 0, min: Infinity, max: -Infinity, lastTime: null };
  let packetsPerSecond = 0;
  let lastSecond = Date.now();
  let packetQueue = [];
  let intervalId = null;

  const rtpSender = dgram.createSocket('udp4');
  rtpSender.isOpen = true;
  rtpSenders.set(channelId, rtpSender);

  function writeAudio(data) {
    if (data.length === 0 || data.every(byte => byte === 0x7F)) {
      logger.warn(`Received empty or silent audio for ${channelId}`);
      return false;
    }
    const freeSpace = maxBufferSize - audioBuffer.length;
    if (data.length > freeSpace) {
      const now = Date.now();
      if (now - lastBufferWarnTime >= 1000) {
        logger.warn(`Buffer full for ${channelId}, discarding ${data.length - freeSpace} bytes`);
        lastBufferWarnTime = now;
      }
      return false;
    }
    audioBuffer = Buffer.concat([audioBuffer, data]);
    return true;
  }

  function sendRtpPacket(packetBuffer) {
    if (!sipMap.has(channelId) || isSocketClosed) {
      logger.info(`Cannot send RTP packet for ${channelId}: channel gone or socket closed`);
      return;
    }
    let offset = 0;
    while (offset < packetBuffer.length) {
      let packetData = packetBuffer.slice(offset, Math.min(offset + samplesPerPacket, packetBuffer.length));
      offset += samplesPerPacket;
      if (packetData.length < samplesPerPacket) {
        packetData = Buffer.concat([packetData, Buffer.alloc(samplesPerPacket - packetData.length, 0x7F)]);
      }
      packetQueue.push({ data: packetData, seq: rtpSequence, timestamp: rtpTimestamp });
      rtpSequence = (rtpSequence + 1) % 65536;
      rtpTimestamp += samplesPerPacket;
    }
    if (!intervalId) {
      processPacketQueue();
    }
  }

  function stopPlayback() {
    if (intervalId) {
      clearInterval(intervalId);
      intervalId = null;
    }
    packetQueue = [];
    logger.info(`Playback stopped for ${channelId}`);
  }

  function processPacketQueue() {
    if (intervalId) {
      return;
    }

    let isFirstPacketAfterResume = !intervalId;
    intervalId = setInterval(() => {
      if (packetQueue.length === 0) {
        clearInterval(intervalId);
        intervalId = null;
        logger.info(`Finished sending delta buffer for ${channelId}, total packets: ${totalPacketsSent}, queue size: ${packetQueue.length}`);
        rtpEvents.emit('audioFinished', channelId);
        return;
      }

      if (!sipMap.has(channelId) || isSocketClosed) {
        logger.info(`Channel ${channelId} gone or socket closed, emitting audioFinished, queue size: ${packetQueue.length}`);
        clearInterval(intervalId);
        intervalId = null;
        rtpEvents.emit('audioFinished', channelId);
        return;
      }

      const packet = packetQueue.shift();
      const startTime = Date.now();
      const header = buildRTPHeader(packet.seq, packet.timestamp, rtpSsrc);
      const rtpPacket = Buffer.concat([header, packet.data]);
      const channelData = sipMap.get(channelId) || {};
      const sendPort = channelData.rtpSource ? channelData.rtpSource.port : rtpSource.port;
      const sendAddress = channelData.rtpSource ? channelData.rtpSource.address : rtpSource.address;

      rtpSender.send(rtpPacket, sendPort, sendAddress, (err) => {
        if (err) {
          logger.error(`Error sending RTP packet for ${channelId} to ${sendAddress}:${sendPort}: ${err.message}`);
        } else {
          totalPacketsSent++;
          totalBytesSent += samplesPerPacket;
          packetsPerSecond++;
          const packetTime = Date.now();
          if (packetTime - lastSecond >= 10000) {
            logger.info(`Packets per second for ${channelId}: ${(packetsPerSecond / 10).toFixed(2)}`);
            packetsPerSecond = 0;
            lastSecond = packetTime;
          }
          if (ptimeStats.lastTime && !isFirstPacketAfterResume) {
            const interval = packetTime - ptimeStats.lastTime;
            if (interval >= 10 && interval <= 60) {
              ptimeStats.count++;
              ptimeStats.sum += interval;
              ptimeStats.min = Math.min(ptimeStats.min, interval);
              ptimeStats.max = Math.min(ptimeStats.max, interval);
            } else if (interval > 60) {
              logger.warn(`Critical ptime deviation: ${interval.toFixed(2)}ms for packet ${totalPacketsSent}, buffer size: ${audioBuffer.length} bytes for ${channelId}`);
            }
          }
          ptimeStats.lastTime = packetTime;
          isFirstPacketAfterResume = false;
        }
      });

      const processingTime = Date.now() - startTime;
      if (processingTime > 5) {
        logger.warn(`High processing time for packet ${totalPacketsSent}: ${processingTime}ms`);
      }
    }, 20);
  }

  function endStream() {
    const avgPtime = ptimeStats.count > 0 ? (ptimeStats.sum / ptimeStats.count).toFixed(2) : 'N/A';
    logger.info(`RTP stream ended for ${channelId}, total packets sent: ${totalPacketsSent}, total bytes: ${totalBytesSent}, final buffer: ${audioBuffer.length} bytes, avg ptime: ${avgPtime}ms`);
    if (intervalId) {
      clearInterval(intervalId);
      intervalId = null;
    }
    if (!isSocketClosed) {
      isSocketClosed = true;
      rtpSender.isOpen = false;
      rtpSender.close();
    }
  }

  return {
    write: writeAudio,
    end: endStream,
    sendRtpPacket: sendRtpPacket,
    stopPlayback: stopPlayback,
    audioBuffer,
    packetQueue
  };
}

module.exports = { startRTPReceiver, getNextRtpPort, releaseRtpPort, streamAudio, rtpEvents };
