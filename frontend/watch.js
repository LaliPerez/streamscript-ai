function getParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function connectSubtitles(roomId, onCaption, onStatus) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/subtitles/${roomId}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'caption') onCaption(msg);
    else if (msg.type === 'status') onStatus(msg);
  };
  ws.onclose = () => onStatus({ type: 'status', status: 'disconnected' });
  return ws;
}

function pickLine(msg, lang) {
  if (!lang || lang === msg.source_lang) return msg.source_text;
  return msg.translations[lang] || msg.source_text;
}
