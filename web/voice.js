// Voice commands: Web Speech API (one-shot or "Hey Meta" wake word), MediaRecorder + /api/transcribe fallback.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const WAKE = /\b(?:hey|hi|hay|ok|okay),?\s*(?:meta|metta|matter|medha|mehta|meda)\b[\s,.!?]*/i;

export const stripWake = t => { const m = t.match(WAKE); return (m ? t.slice(m.index + m[0].length) : t).trim(); };

export class Voice {
  constructor(h) { this.h = h; this.wake = false; this.rec = null; this.armedUntil = 0; this.recording = null; }
  get supported() { return !!SR; }
  state(s, text = '') { this.h.onState?.(s, text); }
  make(continuous) {
    const r = new SR();
    r.lang = navigator.language || 'en-US'; r.continuous = continuous; r.interimResults = true;
    return r;
  }
  err(e) {
    if (e.error === 'no-speech' || e.error === 'aborted') return;
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed') { this.wake = false; this.h.onError?.('Microphone blocked. Allow mic access, or press / to type a command.'); }
    else this.h.onError?.(`Voice: ${e.error}. Press / to type instead.`);
    this.state('idle');
  }
  fire(t) { this.armedUntil = 0; this.state('idle'); if (t) this.h.onCommand(t); }

  // Tap the mic: capture one utterance (wake word optional).
  listenOnce() {
    if (this.recording) return this.recording.stop();
    if (this.wake) { this.armedUntil = Date.now() + 8000; this.state('listening'); return; }
    if (this.rec) return this.rec.stop();
    if (!SR) return this.recordFallback();
    const rec = this.rec = this.make(false);
    let heard = '';
    rec.onresult = e => {
      heard = [...e.results].map(r => r[0].transcript).join(' ');
      this.state('listening', stripWake(heard));
    };
    rec.onerror = e => this.err(e);
    rec.onend = () => { this.rec = null; this.fire(stripWake(heard)); };
    try { rec.start(); this.state('listening'); } catch (e) { this.rec = null; this.h.onError?.('Voice could not start. Press / to type.'); }
  }

  // Wake mode: listen continuously, act on "hey meta <command>".
  setWake(on) {
    if (on && !SR) { this.h.onError?.('The wake word needs Web Speech (Chrome or Safari). Tap the mic instead.'); return false; }
    this.wake = on;
    const r = this.rec; this.rec = null; r?.abort();
    if (on) this.startWake();
    this.state('idle');
    return on;
  }
  startWake() {
    if (!this.wake) return;
    const rec = this.rec = this.make(true);
    rec.onresult = e => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i], text = r[0].transcript, woke = WAKE.test(text), armed = Date.now() < this.armedUntil;
        if (!r.isFinal) { if (woke || armed) this.state('listening', woke ? stripWake(text) : text.trim()); continue; }
        if (woke) {
          const rest = stripWake(text);
          if (rest.length >= 3) this.fire(rest); else { this.armedUntil = Date.now() + 8000; this.state('listening'); }
        } else if (armed) this.fire(text.trim());
      }
    };
    rec.onerror = e => this.err(e);
    rec.onend = () => { if (this.wake && this.rec === rec) setTimeout(() => this.startWake(), 250); };
    try { rec.start(); } catch { /* already started */ }
  }

  // No Web Speech: record a few seconds and let the server transcribe.
  async recordFallback() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { this.h.onError?.('Voice isn’t available here. Press / to type a command.'); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { this.h.onError?.('Microphone blocked. Press / to type a command.'); return; }
    const rec = new MediaRecorder(stream), chunks = [];
    this.recording = rec;
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      clearTimeout(timer);
      this.recording = null;
      stream.getTracks().forEach(t => t.stop());
      this.state('transcribing');
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const ext = blob.type.includes('mp4') ? 'mp4' : blob.type.includes('ogg') ? 'ogg' : 'webm';
        const fd = new FormData(); fd.append('audio', blob, `command.${ext}`);
        const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
        if (!r.ok) throw new Error(r.status);
        const t = stripWake((await r.json()).text || '');
        if (t) this.fire(t); else { this.state('idle'); this.h.onError?.('I didn’t catch that.'); }
      } catch { this.state('idle'); this.h.onError?.('Couldn’t transcribe that. Press / to type your command.'); }
    };
    rec.start();
    this.state('listening', 'Recording… tap to stop');
    const timer = setTimeout(() => rec.state === 'recording' && rec.stop(), 7000);
  }
}
