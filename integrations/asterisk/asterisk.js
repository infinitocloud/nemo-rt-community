const ari = require('ari-client');
const WebSocket = require('ws');
const { config, logger } = require('./config');
const { sipMap, extMap, rtpSenders, rtpReceivers, cleanupPromises } = require('./state');
const { startRTPReceiver, getNextRtpPort, releaseRtpPort } = require('./rtp');
const { startOpenAIWebSocket } = require('./openai');

let ariClient;

async function addExtToBridge(client, channel, bridgeId) {
  try {
    const bridge = await client.bridges.get({ bridgeId });
    await bridge.addChannel({ channel: channel.id });
    logger.info(`ExternalMedia channel ${channel.id} added to bridge ${bridgeId}`);
  } catch (e) {
    logger.error(`Error adding ExternalMedia channel ${channel.id} to bridge ${bridgeId}: ${e.message}`);
    throw e;
  }
}

async function cleanupChannel(channelId) {
  if (cleanupPromises.has(channelId)) {
    await cleanupPromises.get(channelId);
    return;
  }
  if (!sipMap.has(channelId)) {
    logger.info(`Channel ${channelId} not found in sipMap for cleanup`);
    return;
  }
  const cleanupPromise = (async () => {
    const channelData = sipMap.get(channelId);
    sipMap.delete(channelId);
    try {
      logger.info(`Cleanup started for channel ${channelId}`);
      if (channelData.callTimeoutId) {
        clearTimeout(channelData.callTimeoutId);
        logger.info(`Call duration timeout cleared for channel ${channelId}`);
      }
      if (!channelData.wsClosed) {
        logger.info(`WebSocket state for channel ${channelId}: wsClosed=${channelData.wsClosed}`);
        if (channelData.ws && typeof channelData.ws.close === 'function') {
          logger.info(`Forcing WebSocket closure for channel ${channelId}`);
          channelData.ws.close();
        }
        logger.debug(`Waiting for WebSocket closure for ${channelId}`);
        const wsCleanupPromise = new Promise((resolve) => {
          cleanupPromises.set(`ws_${channelId}`, resolve);
          setTimeout(resolve, 1000);
        });
        await wsCleanupPromise;
        logger.info(`WebSocket closure completed for ${channelId}`);
      } else {
        logger.info(`WebSocket already closed for ${channelId}`);
      }
      if (channelData.streamHandler) {
        channelData.streamHandler.end();
        logger.info(`Stream handler ended for ${channelId}`);
      }
      if (channelData.localChannelId) {
        try {
          await ariClient.channels.get({ channelId: channelData.localChannelId });
          await ariClient.channels.hangup({ channelId: channelData.localChannelId });
          logger.info(`Local channel ${channelData.localChannelId} hung up during cleanup`);
        } catch (e) {
          if (e.message.includes('Channel not found') || e.message.includes('Channel not in Stasis')) {
            logger.info(`Local channel ${channelData.localChannelId} already hung up`);
          } else {
            logger.error(`Error hanging up Local channel ${channelData.localChannelId}: ${e.message}`);
          }
        }
      }
      if (channelData.channel && ariClient) {
        try {
          await ariClient.channels.get({ channelId: channelData.channel.id });
          await channelData.channel.hangup();
          logger.info(`Channel ${channelId} hung up`);
        } catch (e) {
          if (e.message.includes('Channel not found') || e.message.includes('Channel not in Stasis')) {
            logger.info(`Channel ${channelId} already terminated or not in Stasis, skipping hangup`);
          } else {
            logger.error(`Hangup error for ${channelId}: ${e.message}`);
          }
        }
      }
      if (channelData.bridge) {
        try {
          await ariClient.bridges.get({ bridgeId: channelData.bridge.id });
          await channelData.bridge.destroy();
          logger.info(`Bridge ${channelData.bridge.id} destroyed`);
        } catch (e) {
          if (e.message.includes('Bridge not found')) {
            logger.info(`Bridge ${channelData.bridge.id} already destroyed, skipping`);
          } else {
            logger.warn(`Failed to destroy bridge: ${e.message}`);
          }
        }
      }
      if (rtpSenders.has(channelId)) {
        const sender = rtpSenders.get(channelId);
        if (sender.isOpen) {
          await new Promise((resolve) => {
            sender.close(() => {
              logger.info(`RTP sender socket closed for ${channelId}`);
              resolve();
            });
            setTimeout(resolve, 1000);
          });
        }
        rtpSenders.delete(channelId);
      }
      if (rtpReceivers.has(channelId)) {
        const receiver = rtpReceivers.get(channelId);
        if (receiver.isOpen) {
          await new Promise((resolve) => {
            receiver.close(() => {
              logger.info(`RTP receiver socket closed for ${channelId}`);
              releaseRtpPort(channelData.rtpPort);
              resolve();
            });
            setTimeout(resolve, 1000);
          });
        }
        rtpReceivers.delete(channelId);
      }
    } catch (e) {
      logger.error(`Cleanup error for ${channelId}: ${e.message}`);
    } finally {
      cleanupPromises.delete(channelId);
      cleanupPromises.delete(`ws_${channelId}`);
    }
  })();
  cleanupPromises.set(channelId, cleanupPromise);
  await cleanupPromise;
}

async function initializeAriClient() {
  try {
    ariClient = await ari.connect(config.ARI_URL, config.ARI_USER, config.ARI_PASS);
    logger.info(`Connected to ARI at ${config.ARI_URL}`);
    await ariClient.start(config.ARI_APP);
    logger.info(`ARI application "${config.ARI_APP}" started`);

    ariClient.on('StasisStart', async (evt, channel) => {
      logger.info(`StasisStart for channel ${channel.id}, name: ${channel.name}`);
      if (channel.name && channel.name.startsWith('Local/')) {
        logger.info(`Ignoring Local channel ${channel.id}, name: ${channel.name}`);
        return;
      }
      if (channel.name && channel.name.startsWith('UnicastRTP')) {
        logger.info(`ExternalMedia channel started: ${channel.id}`);
        let mapping = extMap.get(channel.id);
        let attempts = 0;
        const maxAttempts = 10;
        while (!mapping && attempts < maxAttempts) {
          await new Promise(resolve => setTimeout(resolve, 50));
          mapping = extMap.get(channel.id);
          attempts++;
        }
        if (mapping) {
          await addExtToBridge(ariClient, channel, mapping.bridgeId);
          logger.info(`Bridge ${mapping.bridgeId} ready for audio routing, external channel ${channel.id} active with codec ulaw`);
        } else {
          logger.error(`No mapping found for ExternalMedia channel ${channel.id} after ${maxAttempts} attempts`);
        }
        return;
      }
      logger.info(`SIP channel started: ${channel.id}`);
      try {
        const bridgeId = `${channel.id}_bridge`;
        const bridge = await ariClient.bridges.create({ type: 'mixing,proxy_media', bridgeId });
        await bridge.addChannel({ channel: channel.id });
        await channel.answer();
        logger.info(`Channel ${channel.id} answered, bridge ${bridgeId} created for SIP audio`);

        const port = getNextRtpPort();
        await startRTPReceiver(channel.id, port);
        const extParams = {
          app: config.ARI_APP,
          external_host: `127.0.0.1:${port}`,
          format: 'ulaw',
          transport: 'udp',
          encapsulation: 'rtp',
          connection_type: 'client',
          direction: 'both'
        };
        sipMap.set(channel.id, { bridgeId, channelId: channel.id, bridge, channel, rtpPort: port, wsClosed: false });
        const extChannel = await ariClient.channels.externalMedia(extParams);
        logger.info(`ExternalMedia channel ${extChannel.id} created with codec ulaw, RTP to 127.0.0.1:${port}`);
        extMap.set(extChannel.id, { bridgeId, channelId: channel.id });
        logger.info(`extMap updated for channel ${extChannel.id} with bridge ${bridgeId}`);

        if (config.CALL_DURATION_LIMIT_SECONDS > 0) {
          const channelData = sipMap.get(channel.id);
          channelData.callTimeoutId = setTimeout(async () => {
            logger.info(`Call duration limit of ${config.CALL_DURATION_LIMIT_SECONDS} seconds reached for channel ${channel.id}, hanging up`);
            try {
              await ariClient.channels.hangup({ channelId: channel.id });
              logger.info(`Channel ${channel.id} hung up due to duration limit`);
            } catch (e) {
              logger.error(`Error hanging up channel ${channel.id} due to duration limit: ${e.message}`);
            }
          }, config.CALL_DURATION_LIMIT_SECONDS * 1000);
          sipMap.set(channel.id, channelData);
        }

        await startOpenAIWebSocket(channel.id);
      } catch (e) {
        logger.error(`Error in SIP channel ${channel.id}: ${e.message}`);
        await cleanupChannel(channel.id);
      }
    });

    ariClient.on('StasisEnd', async (evt, channel) => {
      logger.info(`StasisEnd for channel ${channel.id}, name: ${channel.name}`);
      if (channel.name && channel.name.startsWith('UnicastRTP')) {
        extMap.delete(channel.id);
        logger.info(`ExternalMedia channel ${channel.id} removed`);
      } else if (channel.name && channel.name.startsWith('Local/')) {
        logger.info(`Local channel ${channel.id} ended, no cleanup needed`);
      } else {
        try {
          await ariClient.channels.get({ channelId: channel.id });
          logger.info(`Channel ${channel.id} still active, skipping cleanup`);
        } catch (e) {
          if (e.message.includes('Channel not found')) {
            logger.info(`Channel ${channel.id} already hung up, no cleanup needed`);
            const channelData = sipMap.get(channel.id);
            if (channelData && channelData.ws && !channelData.wsClosed && typeof channelData.ws.close === 'function') {
              logger.info(`Closing WebSocket for channel ${channel.id} on StasisEnd`);
              channelData.ws.close();
            }
            await cleanupChannel(channel.id);
          } else {
            logger.error(`Error checking channel ${channel.id} state: ${e.message}`);
          }
        }
      }
    });

    process.on('SIGINT', async () => {
      logger.info('Received SIGINT, cleaning up...');
      const channelsToClean = [...sipMap.keys()];
      const cleanupTasks = channelsToClean.map(cleanupChannel);
      await Promise.all([...cleanupPromises.values(), ...cleanupTasks]);
      sipMap.clear();
      extMap.clear();
      cleanupPromises.clear();
      if (ariClient) {
        try {
          await ariClient.stop();
          logger.info('ARI client stopped');
        } catch (e) {
          logger.error(`Error stopping ARI client: ${e.message}`);
        }
      }
      logger.info('Cleanup completed');
      process.exit(0);
    });
  } catch (e) {
    logger.error(`ARI connection error: ${e.message}`);
    process.exit(1);
  }
}

module.exports = { initializeAriClient, ariClient };
