// Voice commands: Web Speech API (one-shot or "Hey Meta" wake word), MediaRecorder + /api/transcribe fallback.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const WAKE = /\b(?:hey|hi|hay|ok|okay),?\s*(?:meta|metta|matter|medha|mehta|meda)\b[\s,.!?]*/i;

export const stripWake = t => { const m = t.match(WAKE); return (m ? t.slice(m.index + m[0].length) : t).trim(); };
// "Hey Meta" on its own still means "help me".
const commandFrom = t => { const c = stripWake(t); return c || (WAKE.test(t) ? 'help me' : ''); };
const SILENCE_MS = 1300, MAX_MS = 9000, START_GRACE_MS = 4000;

export class Voice {
  // serverVoice: the server has Muse Voice Transcribe (set once /api/health says online). It works in
  // every browser, including iPad Safari and the home-screen app where Web Speech often fails.
  constructor(h) { this.h = h; this.wake = false; this.rec = null; this.armedUntil = 0; this.recording = null; this.serverVoice = false; }
  get supported() { return !!SR; }
  state(s, text = '') { this.h.onState?.(s, text); }
  make(continuous) {
    const r = new SR();
    r.lang = navigator.language || 'en-US'; r.continuous = continuous; r.interimResults = true;
    return r;
  }
  err(e) {
    if (e.error === 'aborted') return;
    this.failed = true;
    if (e.error === 'no-speech') { this.h.onError?.('I didn’t hear anything. Tap the mic and speak, or press / to type.'); this.state('idle'); return; }
    if (e.error === 'network') { this.wake = false; this.h.onError?.('This browser’s speech service is unreachable. Tap the mic again: Meta will transcribe instead.'); this.serverVoice = true; this.state('idle'); return; }
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
    if (this.serverVoice || !SR) return this.recordFallback();
    this.browserOnce();
  }
  browserOnce() {
    const rec = this.rec = this.make(false);
    let heard = '';
    rec.onresult = e => {
      heard = [...e.results].map(r => r[0].transcript).join(' ');
      this.state('listening', stripWake(heard));
    };
    rec.onerror = e => this.err(e);
    rec.onend = () => {
      this.rec = null;
      const cmd = commandFrom(heard);
      if (cmd) this.fire(cmd); else if (!this.failed) { this.state('idle'); this.h.onError?.('I didn’t hear anything. Tap the mic and speak, or press / to type.'); }
      this.failed = false;
    };
    this.failed = false;
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
    // Make the audio context inside the tap itself: iPad Safari keeps one made later suspended.
    const AC = window.AudioContext || window.webkitAudioContext;
    let ctx = null; try { ctx = AC ? new AC() : null; ctx?.resume?.(); } catch { ctx = null; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { ctx?.close?.(); this.h.onError?.('Microphone blocked. Allow the mic for this site, or press / to type.'); return; }
    const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(t => MediaRecorder.isTypeSupported?.(t));
    const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined), chunks = [];
    this.recording = rec;
    const stopSilence = this.watchSilence(ctx, stream, () => rec.state === 'recording' && rec.stop());
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      clearTimeout(timer);
      stopSilence();
      this.recording = null;
      stream.getTracks().forEach(t => t.stop());
      this.state('transcribing');
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const ext = blob.type.includes('mp4') ? 'mp4' : blob.type.includes('ogg') ? 'ogg' : 'webm';
        const fd = new FormData(); fd.append('audio', blob, `command.${ext}`);
        const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
        if (!r.ok) throw new Error(r.status);
        const t = commandFrom((await r.json()).text || '');
        if (t) this.fire(t); else { this.state('idle'); this.h.onError?.('I didn’t catch that. Tap the mic and try again.'); }
      } catch (err) {
        console.warn('[margin] transcribe failed:', err);
        this.state('idle');
        if (SR && this.serverVoice) {             // server voice failed: use the browser's speech next time
          this.serverVoice = false;
          this.h.onError?.('Meta couldn’t transcribe that. Tap the mic again to use the browser’s speech.');
        } else this.h.onError?.('Couldn’t transcribe that. Press / to type your command.');
      }
    };
    rec.start(250);
    this.state('listening', 'Listening… (tap to stop)');
    const timer = setTimeout(() => rec.state === 'recording' && rec.stop(), MAX_MS);
  }

  // Stop recording after a pause in speech, so the student just talks (no second tap).
  // If the context never starts, only the tap or the time limit ends the recording.
  watchSilence(ctx, stream, onSilence) {
    if (!ctx) return () => {};
    let raf;
    try {
      const src = ctx.createMediaStreamSource(stream), an = ctx.createAnalyser();
      an.fftSize = 1024; src.connect(an);
      const buf = new Uint8Array(an.fftSize), t0 = performance.now();
      let spoke = false, quietSince = 0;
      const tick = () => {
        an.getByteTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += (v - 128) ** 2;
        const rms = Math.sqrt(sum / buf.length), now = performance.now();
        if (ctx.state !== 'running') { raf = requestAnimationFrame(tick); return; }
        if (rms > 6) { spoke = true; quietSince = 0; }
        else if (spoke) { quietSince ||= now; if (now - quietSince > SILENCE_MS) return onSilence(); }
        else if (now - t0 > START_GRACE_MS) return onSilence();
        raf = requestAnimationFrame(tick);
      };
      ctx.resume?.(); raf = requestAnimationFrame(tick);
    } catch { ctx.close?.(); return () => {}; }
    return () => { cancelAnimationFrame(raf); ctx?.close?.(); };
  }
}
