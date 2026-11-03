// Voice dictation into the command bar, like a dictation button: tap the mic to turn it on, words appear
// in the bar, tap again (or press Enter) to stop. It stops by itself only after a minute of silence.
//
// Two engines:
//   browser: Web Speech (webkitSpeechRecognition). Words appear live as you speak. Chrome, Edge, Safari.
//   meta:    Muse Voice Transcribe. Live over a WebSocket (/ws/voice) where the server can hold one, so
//            words appear as you speak; otherwise (Vercel) record a clip and send it when the mic turns off.
// The browser engine is the default when it exists; if it can't run here it switches to Meta by itself.
// "Hey Meta" wake mode (continuous listening for the wake word) uses the browser engine.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const WAKE = /\b(?:hey|hi|hay|ok|okay),?\s*(?:meta|metta|matter|medha|mehta|meda)\b[\s,.!?]*/i;
export const SILENCE_MS = 10000;                     // turn off after 10 s with no speech
const META_MAX_MS = 120000;                          // 2 min of 16 kHz WAV is ~3.8 MB, under Vercel's 4.5 MB body limit
const WAV_RATE = 16000;
const LIVE_RATE = 24000;                             // Meta live voice takes PCM_24KHZ
const ENGINE_KEY = 'margin.voiceEngine';
// Web Speech errors that mean "this browser can't do speech here": use Meta instead.
const SWITCH_ERRORS = new Set(['network', 'service-not-allowed', 'language-not-supported']);
const UPLOAD_ERRORS = {
  400: 'Meta couldn’t understand the recording.',
  411: 'The upload was refused (no length).',
  413: 'That recording was too long.',
  415: 'Meta needs WAV audio and this recording wasn’t one.',
  503: 'Meta voice is unavailable (is MODEL_API_KEY set on the server?).',
};

export const stripWake = t => { const m = t.match(WAKE); return (m ? t.slice(m.index + m[0].length) : t).trim(); };
const clean = t => stripWake(t.replace(/\s+/g, ' ')).replace(/^[,.!?\s]+/, '');

// Muse Voice Transcribe takes only 16-bit mono PCM WAV at 16 or 24 kHz, and browsers record
// webm/opus (Chrome) or mp4/aac (Safari). Decode the recording and write it out as WAV.
// The recording is sent exactly as the mic heard it: boosting or trimming quiet audio distorted it
// and Meta misheard it (a whole sentence came back as "Sorry").
export const QUIET_PEAK = 0.003;                     // below this the mic recorded silence

// Is this speech? Quiet laptop mics and soft voices sit far below a fixed threshold, so the bar is
// set from the room itself: a little above the background noise heard so far (it learns the floor
// in the first half second and keeps following it), never below a tiny absolute minimum.
export class SpeechGate {
  constructor() { this.floor = null; this.n = 0; }
  // rms: loudness of one short frame (0..1). Returns true when the frame is speech.
  hear(rms) {
    this.n++;
    if (this.floor == null) this.floor = rms;
    else if (rms < this.floor) this.floor = this.floor * 0.7 + rms * 0.3;     // quieter: follow down fast
    else this.floor = this.floor * 0.995 + rms * 0.005;                      // louder: follow up slowly
    return this.n > 3 && rms > Math.max(this.floor * 2.2, 0.004);
  }
}

export async function toWav(blob) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = new AC();
  try {
    const bytes = await blob.arrayBuffer();
    const buf = await new Promise((ok, bad) => { const p = ctx.decodeAudioData(bytes, ok, bad); p?.then?.(ok, bad); });
    const chans = [...Array(buf.numberOfChannels)].map((_, c) => buf.getChannelData(c));
    const step = buf.sampleRate / WAV_RATE, n = Math.floor(buf.length / step);
    const mono = new Float32Array(n);
    let peak = 0;
    for (let i = 0; i < n; i++) {                      // average each window: mono and anti-aliased
      const a = Math.floor(i * step), b = Math.max(a + 1, Math.floor((i + 1) * step));
      let sum = 0; for (const ch of chans) for (let j = a; j < b; j++) sum += ch[j] || 0;
      const v = sum / ((b - a) * chans.length);
      mono[i] = v; if (Math.abs(v) > peak) peak = Math.abs(v);
    }
    const from = 0, len = n;                           // sent as recorded: no trimming, no boost
    const out = new DataView(new ArrayBuffer(44 + len * 2));
    const str = (o, t) => [...t].forEach((ch, i) => out.setUint8(o + i, ch.charCodeAt(0)));
    str(0, 'RIFF'); out.setUint32(4, 36 + len * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
    out.setUint32(24, WAV_RATE, true); out.setUint32(28, WAV_RATE * 2, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
    str(36, 'data'); out.setUint32(40, len * 2, true);
    for (let i = 0; i < len; i++) {
      const v = Math.max(-1, Math.min(1, mono[from + i]));
      out.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    const wav = new Blob([out.buffer], { type: 'audio/wav' });
    wav.peak = peak; wav.seconds = len / WAV_RATE; wav.recorded = n / WAV_RATE;
    return wav;
  } finally { ctx.close?.(); }
}

export class Voice {
  // h: onText(text) puts words in the bar, onState(state, detail) with state 'on' | 'off' | 'transcribing',
  //    onError(msg) shows a problem on screen, onWake(on), onCommand(text) for wake-word commands.
  constructor(h) {
    this.h = h; this.on = false; this.wake = false; this.rec = null; this.recording = null; this.serverOk = undefined;
    let saved = null; try { saved = localStorage.getItem(ENGINE_KEY); } catch { /* private mode */ }
    this.engine = SR ? (saved === 'meta' ? 'meta' : 'browser') : 'meta';
  }
  get supported() { return !!SR || !!window.MediaRecorder; }
  get canChoose() { return !!SR; }
  get busy() { return this.on; }
  get engineName() { return this.engine === 'browser' ? 'Browser speech' : 'Meta Voice Transcribe'; }
  setEngine(e, remember = true) {
    if (e === 'browser' && !SR) return;
    if (this.on) this.stop();
    this.engine = e;
    if (remember) try { localStorage.setItem(ENGINE_KEY, e); } catch { /* private mode */ }
    this.state(this.on ? 'on' : 'off');
  }
  log(msg) { this.trail = [...(this.trail || []).slice(-3), msg]; this.h.onLog?.(`${this.engine === 'browser' ? 'browser speech' : 'Meta'}: ${this.trail.join(' → ')}`); }
  state(s, detail = '') { this.h.onState?.(s, detail || this.engineName); }
  fail(msg) { console.warn('[margin] voice:', msg); this.h.onError?.(msg); }

  toggle() { return this.on ? this.stop() : this.start(); }
  listenOnce() { return this.toggle(); }              // older name

  start(prefix = '') {
    if (this.on) return;
    if (this.wake) this.setWake(false);
    this.prefix = prefix ? prefix.trim() + ' ' : '';
    this.on = true;
    this.lastSpeech = Date.now();
    clearInterval(this.watch);
    this.watch = setInterval(() => { if (this.on && Date.now() - this.lastSpeech > SILENCE_MS) { this.stop(); this.h.onError?.('Mic turned off after 10 seconds of silence.'); } }, 1000);
    if (this.engine === 'browser') this.startBrowser(); else this.startMeta();
  }
  stop() {
    if (!this.on) return;
    this.on = false;
    clearInterval(this.watch);
    this.log('mic off');
    if (this.liveStop) { this.liveStop(); return; }                                 // live socket: Meta sends the last words
    if (this.recording?.state === 'recording') { this.recording.stop(); return; }   // onstop transcribes
    const r = this.rec; this.rec = null;
    try { r?.stop(); } catch { /* not started */ }
    // Safari sometimes never ends a stopped session (the mic stays live): force it after a moment.
    if (r) setTimeout(() => { try { r.abort(); } catch { /* already ended */ } }, 1200);
    this.state('off');
  }

  // ---- browser engine: live words ---------------------------------------------------------------
  startBrowser() {
    this.finalText = '';
    const run = () => {
      if (!this.on) return;
      const rec = this.rec = new SR();
      rec.lang = navigator.language || 'en-US'; rec.continuous = true; rec.interimResults = true;
      let sessionFinal = '';
      rec.onaudiostart = () => this.log('listening');
      rec.onspeechstart = () => { this.lastSpeech = Date.now(); this.log('hearing speech'); };
      rec.onresult = e => {
        this.lastSpeech = Date.now();
        let interim = '';
        sessionFinal = '';
        for (let i = 0; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) sessionFinal += t + ' '; else interim += t;
        }
        this.h.onText?.(clean(this.prefix + this.finalText + sessionFinal + interim));
      };
      rec.onerror = e => {
        this.log(`error ${e.error}`);
        if (e.error === 'aborted' || e.error === 'no-speech') return;    // silence: the 10 s timer decides
        if (SWITCH_ERRORS.has(e.error) && window.MediaRecorder) {
          this.on = false; this.rec = null;
          this.setEngine('meta', false);
          this.start(this.prefix + this.finalText + sessionFinal);
          this.h.onError?.(`Browser speech can’t run here (${e.error}). Switched to Meta Voice Transcribe: words appear when you turn the mic off.`);
          return;
        }
        this.stop();
        if (e.error === 'not-allowed') this.fail('Microphone blocked. Allow the mic for this site (Safari: aA › Website Settings › Microphone).');
        else if (e.error === 'audio-capture') this.fail('No microphone found.');
        else this.fail(`Browser speech error: ${e.error}. Try “Use Meta” in the bar.`);
      };
      // Safari and Chrome end a session after a while or a pause; keep going until the mic is turned off.
      rec.onend = () => {
        this.finalText += sessionFinal;
        if (this.on && this.rec === rec) setTimeout(run, 150);
      };
      try { rec.start(); } catch (err) { this.stop(); this.fail(`Voice could not start: ${err.message || err}`); }
    };
    run();
    this.state('on');
  }

  // ---- meta engine: record, then transcribe ------------------------------------------------------
  async serverReady() {
    if (this.serverOk === undefined) {
      try { const h = await (await fetch('/api/health')).json(); this.serverOk = !h.offline; this.realtime = !!h.realtime && !!window.WebSocket; }
      catch { return false; }
    }
    return this.serverOk;
  }
  async startMeta() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { this.on = false; this.fail('Recording isn’t available in this browser. Press / to type.'); return; }
    // Make the audio context inside the tap itself: iPad Safari keeps one made later suspended.
    const AC = window.AudioContext || window.webkitAudioContext;
    let ctx = null; try { ctx = AC ? new AC() : null; ctx?.resume?.(); } catch { ctx = null; }
    this.state('on', 'Meta Voice Transcribe');
    if (!(await this.serverReady())) {
      ctx?.close?.(); this.on = false; this.state('off');
      this.fail('Meta is offline on this server (no MODEL_API_KEY), so it can’t transcribe. ' + (SR ? 'Switch to browser speech in the bar.' : 'Press / to type.'));
      return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: true } }); }
    catch (err) { ctx?.close?.(); this.on = false; this.state('off'); this.fail(`Microphone blocked (${err.name || 'error'}). Allow the mic for this site.`); return; }
    if (!this.on) { stream.getTracks().forEach(t => t.stop()); ctx?.close?.(); return; }   // turned off while asking
    if (this.realtime && ctx) {
      try { await this.startLive(ctx, stream); return; }
      catch (err) { this.log(`live voice unavailable (${err.message}), recording instead`); }
      if (!this.on) { stream.getTracks().forEach(t => t.stop()); ctx?.close?.(); return; }
    }
    const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(t => MediaRecorder.isTypeSupported?.(t));
    const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined), chunks = [];
    this.recording = rec;
    const stopLevel = this.watchLevel(ctx, stream);
    const cap = setTimeout(() => { if (rec.state === 'recording') { this.h.onError?.('Two minutes is the most Meta can take at once. Transcribing now.'); this.stop(); } }, META_MAX_MS);
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      clearTimeout(cap); stopLevel();
      this.recording = null; this.on = false;
      stream.getTracks().forEach(t => t.stop());
      this.state('transcribing', 'Meta is transcribing…');
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        if (!blob.size) throw new Error('The microphone recorded nothing. Check the mic permission for this site.');
        let wav;
        try { wav = await toWav(blob); }
        catch { throw new Error(`This browser’s recording (${blob.type || 'unknown type'}) couldn’t be converted to WAV for Meta.`); }
        const level = Math.round(wav.peak * 100);
        this.log(`mic level ${level}%, ${wav.seconds.toFixed(1)} s of sound`);
        if (wav.peak < QUIET_PEAK) throw new Error(`The microphone recorded silence (level ${level}%). Check that the right mic is selected in your computer’s sound settings and in the browser’s site settings.`);
        this.log(`sending ${Math.round(wav.size / 1024)} KB WAV`);
        const fd = new FormData(); fd.append('audio', wav, 'command.wav');
        const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
        if (!r.ok) {
          const detail = await r.json().then(j => j.detail, () => '');
          throw new Error(`Meta voice failed (HTTP ${r.status}): ${detail || UPLOAD_ERRORS[r.status] || 'server error'}`);
        }
        const body = await r.json();
        this.log(`Meta replied${body.audio_ms != null ? ` (heard ${(body.audio_ms / 1000).toFixed(1)} s)` : ''}`);
        const text = clean(this.prefix + (body.text || ''));
        if (text) this.h.onText?.(text);
        else if (body.unclear) throw new Error(`Meta only caught “${body.unclear}” from ${wav.seconds.toFixed(1)} s of audio, so it probably misheard. Try again a little closer to the mic` + (SR ? ', or use Browser speech in the bar.' : '.'));
        else throw new Error(`Meta got ${wav.seconds.toFixed(1)} s of audio (level ${level}%) but found no words${body.session ? ` (session ${body.session})` : ''}. ` + (SR ? 'Try Browser speech in the bar instead.' : 'Try again, speaking a little louder.'));
      } catch (err) {
        this.fail(err.message || String(err));
      } finally { this.state('off'); }
    };
    rec.start(250);
    this.log(`recording (${rec.mimeType || 'default format'})`);
  }

  // ---- Meta live: stream the mic over a WebSocket, words come back as you speak ------------------
  // Resolves once Meta accepts the session; rejects (so the caller records a clip instead) if it can't.
  startLive(ctx, stream) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/voice`);
      ws.binaryType = 'arraybuffer';
      let ready = false, finalText = '', partial = '', proc = null, src = null, done = false;
      const fail = msg => { clearTimeout(wait); stopCapture(); try { ws.close(); } catch { /* closed */ } reject(new Error(msg)); };
      const wait = setTimeout(() => !ready && fail('no answer in 6 s'), 6000);
      const words = () => clean(this.prefix + finalText + partial);
      const stopCapture = () => { try { proc?.disconnect(); src?.disconnect(); } catch { /* gone */ } proc = src = null; };
      const finish = () => {
        if (done) return; done = true;
        stopCapture(); clearTimeout(this.liveFlush);
        this.liveStop = null; this.on = false;
        stream.getTracks().forEach(t => t.stop()); ctx.close?.();
        const text = words();
        if (text) { this.h.onText?.(text); this.log('done'); }
        else this.fail(SR ? 'Meta live heard no words. Try again a little louder, or use Browser speech in the bar.' : 'Meta live heard no words. Try again a little louder.');
        this.state('off');
      };
      const capture = () => {
        // ScriptProcessor works in every browser (AudioWorklet needs another file); resample to 24 kHz.
        src = ctx.createMediaStreamSource(stream);
        proc = ctx.createScriptProcessor(2048, 1, 1);
        const step = ctx.sampleRate / LIVE_RATE;
        const gate = new SpeechGate();   // only decides when the speaker has gone quiet
        let pos = 0;
        proc.onaudioprocess = e => {
          const inp = e.inputBuffer.getChannelData(0);
          const n = Math.floor((inp.length - pos) / step);
          if (n <= 0 || ws.readyState !== 1) return;
          const mono = new Float32Array(n);
          let peak = 0, sq = 0;
          for (let i = 0; i < n; i++) {
            const a = Math.floor(pos + i * step), b = Math.max(a + 1, Math.floor(pos + (i + 1) * step));
            let sum = 0; for (let j = a; j < b && j < inp.length; j++) sum += inp[j];
            const v = sum / (b - a);
            mono[i] = v; sq += v * v;
            if (Math.abs(v) > peak) peak = Math.abs(v);
          }
          pos = pos + n * step - inp.length;
          if (gate.hear(Math.sqrt(sq / n))) this.lastSpeech = Date.now();
          const out = new Int16Array(n);                // raw audio, as the mic heard it
          for (let i = 0; i < n; i++) { const v = Math.max(-1, Math.min(1, mono[i])); out[i] = v < 0 ? v * 0x8000 : v * 0x7fff; }
          ws.send(out.buffer);
        };
        src.connect(proc); proc.connect(ctx.destination);   // Chrome only runs a connected processor
        ctx.resume?.();
      };
      ws.onmessage = e => {
        let m; try { m = JSON.parse(e.data); } catch { return; }
        if (m.type === 'ready') {
          ready = true; clearTimeout(wait);
          if (!this.on) { ws.close(); finish(); resolve(); return; }
          this.log('live: connected, speak now');
          capture();
          this.state('on', 'Meta live voice');
          this.liveStop = () => {
            stopCapture(); this.on = false; clearInterval(this.watch);
            this.state('transcribing', 'Meta is finishing…');
            try { ws.send(JSON.stringify({ type: 'endStream' })); } catch { /* closed */ }
            this.liveFlush = setTimeout(() => { try { ws.close(); } catch { /* closed */ } finish(); }, 8000);
          };
          resolve();
        } else if (m.type === 'transcript' || m.type === 'speechComplete') {
          this.lastSpeech = Date.now();
          if (m.final) { finalText += m.transcript + ' '; partial = ''; } else partial = m.transcript;
          this.h.onText?.(words());
        } else if (m.type === 'error') {
          if (!ready) fail(m.message); else this.log(`error: ${m.message}`);
        }
      };
      ws.onerror = () => { if (!ready) fail('socket error'); };
      ws.onclose = () => { if (!ready) fail('socket closed'); else finish(); };
    });
  }

  // Keep the one-minute silence timer fed while the student talks (Meta engine has no live words).
  watchLevel(ctx, stream) {
    if (!ctx) { this.lastSpeech = Infinity; return () => {}; }      // can't measure: only the tap or the 2 min cap stops it
    let raf;
    try {
      const src = ctx.createMediaStreamSource(stream), an = ctx.createAnalyser();
      an.fftSize = 1024; src.connect(an);
      const buf = new Float32Array(an.fftSize), gate = new SpeechGate();
      const tick = () => {
        an.getFloatTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += v * v;
        if (ctx.state === 'running' && gate.hear(Math.sqrt(sum / buf.length))) this.lastSpeech = Date.now();
        raf = requestAnimationFrame(tick);
      };
      ctx.resume?.(); raf = requestAnimationFrame(tick);
    } catch { ctx.close?.(); this.lastSpeech = Infinity; return () => {}; }
    return () => { cancelAnimationFrame(raf); ctx?.close?.(); };
  }

  // ---- "Hey Meta" wake mode (browser engine only) -------------------------------------------------
  setWake(on) {
    if (on && !SR) { this.fail('The wake word needs browser speech (Chrome or Safari). Tap the mic instead.'); return false; }
    if (on && this.on) this.stop();
    this.wake = on;
    const r = this.rec; this.rec = null; try { r?.abort(); } catch { /* not started */ }
    if (on) this.startWake();
    return on;
  }
  stopWake() { if (this.wake) { this.setWake(false); this.h.onWake?.(false); } }
  startWake() {
    if (!this.wake) return;
    const rec = this.rec = new SR();
    rec.lang = navigator.language || 'en-US'; rec.continuous = true; rec.interimResults = false;
    rec.onresult = e => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const text = e.results[i][0].transcript;
        if (!e.results[i].isFinal || !WAKE.test(text)) continue;
        const rest = stripWake(text);
        if (rest.length >= 3) this.h.onCommand?.(rest);
        else { this.stopWake(); this.start(); }        // "Hey Meta" alone: open the mic for the command
      }
    };
    rec.onerror = e => {
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      this.stopWake();
      this.fail(e.error === 'not-allowed' ? 'Microphone blocked. Allow mic access for the wake word.' : `The wake word can’t run here (${e.error}). Tap the mic instead.`);
    };
    rec.onend = () => { if (this.wake && this.rec === rec) setTimeout(() => this.startWake(), 250); };
    try { rec.start(); } catch { /* already started */ }
  }
}
