require('dotenv').config({ path: './config.conf' });
const winston = require('winston');
const chalk = require('chalk');

// Define configuration object
const config = {
  ARI_URL: process.env.ARI_URL || 'http://127.0.0.1:8088',
  ARI_USER: process.env.ARI_USERNAME,
  ARI_PASS: process.env.ARI_PASSWORD,
  ARI_APP: 'asterisk_to_openai_rt',
  OPENAI_API_KEY: process.env.OPENAI_API_KEY,
  // GA endpoint. Override REALTIME_URL to point at a self-hosted OpenAI-compatible
  // server (e.g. Nemo-RT: ws://127.0.0.1:8000/v1/realtime). Defaults to OpenAI GA.
  REALTIME_URL: process.env.REALTIME_URL || `wss://api.openai.com/v1/realtime?model=${process.env.REALTIME_MODEL || 'gpt-realtime'}`,
  OPENAI_VOICE: process.env.OPENAI_VOICE || 'alloy',
  // Whisper transcription language for the USER's speech (log/display only).
  // 'es' = español, 'en' = English. Leave empty for auto-detect (bilingual).
  TRANSCRIPTION_LANGUAGE: process.env.TRANSCRIPTION_LANGUAGE || 'en',
  RTP_PORT_START: 12000,
  MAX_CONCURRENT_CALLS: parseInt(process.env.MAX_CONCURRENT_CALLS) || 10,
  VAD_THRESHOLD: parseFloat(process.env.VAD_THRESHOLD) || 0.6,
  VAD_PREFIX_PADDING_MS: Number(process.env.VAD_PREFIX_PADDING_MS) || 200,
  VAD_SILENCE_DURATION_MS: Number(process.env.VAD_SILENCE_DURATION_MS) || 600,
  LOG_LEVEL: process.env.LOG_LEVEL || 'info',
  SYSTEM_PROMPT: process.env.SYSTEM_PROMPT,
  INITIAL_MESSAGE: process.env.INITIAL_MESSAGE || 'Hi',
  SILENCE_PADDING_MS: parseInt(process.env.SILENCE_PADDING_MS) || 100,
  CALL_DURATION_LIMIT_SECONDS: parseInt(process.env.CALL_DURATION_LIMIT_SECONDS) || 0 // 0 means no limit
};

// Debug logging of loaded configuration
console.log('Loaded configuration:', {
  ARI_URL: config.ARI_URL,
  ARI_USER: config.ARI_USER,
  ARI_PASS: config.ARI_PASS ? 'set' : 'unset',
  OPENAI_API_KEY: config.OPENAI_API_KEY ? 'set' : 'unset',
  LOG_LEVEL: config.LOG_LEVEL,
  SYSTEM_PROMPT: config.SYSTEM_PROMPT ? 'set' : 'unset'
});

// Logger configuration
let sentEventCounter = 0;
let receivedEventCounter = -1;
const logger = winston.createLogger({
  level: config.LOG_LEVEL,
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.printf(({ timestamp, level, message }) => {
      const [origin] = message.split(' ', 1);
      let counter, coloredMessage;
      if (origin === '[Client]') {
        counter = `C-${sentEventCounter.toString().padStart(4, '0')}`;
        sentEventCounter++;
        coloredMessage = chalk.cyanBright(message);
      } else if (origin === '[OpenAI]') {
        counter = `O-${receivedEventCounter.toString().padStart(4, '0')}`;
        receivedEventCounter++;
        coloredMessage = chalk.yellowBright(message);
      } else {
        counter = 'N/A';
        coloredMessage = chalk.gray(message);
      }
      return `${counter} | ${timestamp} [${level.toUpperCase()}] ${coloredMessage}`;
    })
  ),
  transports: [
    new winston.transports.Console()
  ]
});

// Validate critical configurations
if (!config.SYSTEM_PROMPT || config.SYSTEM_PROMPT.trim() === '') {
  logger.error('SYSTEM_PROMPT is missing or empty in config.conf');
  process.exit(1);
}
logger.info('SYSTEM_PROMPT loaded from config.conf');

if (config.CALL_DURATION_LIMIT_SECONDS < 0) {
  logger.error('CALL_DURATION_LIMIT_SECONDS cannot be negative in config.conf');
  process.exit(1);
}
logger.info(`CALL_DURATION_LIMIT_SECONDS set to ${config.CALL_DURATION_LIMIT_SECONDS} seconds`);

const logClient = (msg, level = 'info') => logger[level](`[Client] ${msg}`);
const logOpenAI = (msg, level = 'info') => logger[level](`[OpenAI] ${msg}`);

module.exports = {
  config,
  logger,
  logClient,
  logOpenAI
};
