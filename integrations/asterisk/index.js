const { initializeAriClient } = require('./asterisk');
const { config, logger } = require('./config');

async function startApplication() {
  try {
    logger.info('Starting application');
    await initializeAriClient();
    logger.info('Application started successfully');
  } catch (e) {
    logger.error(`Startup error: ${e.message}`);
    process.exit(1);
  }
}

startApplication();
