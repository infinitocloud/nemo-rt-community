const sipMap = new Map();
const extMap = new Map();
const rtpSenders = new Map();
const rtpReceivers = new Map();
const cleanupPromises = new Map();

module.exports = {
  sipMap,
  extMap,
  rtpSenders,
  rtpReceivers,
  cleanupPromises
};
