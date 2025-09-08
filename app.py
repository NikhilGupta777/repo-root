#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Trimmer – robust single-file app
- Paste a YouTube/direct link
- Mark In/Out (or type: "10:03-11:04", "start 1:20 end +45s", "1h2m → 1h2m30s", "90-150", "+30s")
- Progress for Download + Trim
- Cancel running job
- Output: MP4 (H.264 + AAC) with +faststart
"""

import os, re, json, time, uuid, shutil, tempfile, subprocess, threading, signal
from pathlib import Path
from typing import Optional, Tuple
from flask import Flask, request, jsonify, Response, send_from_directory, abort



# ---------- Config ----------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
APP_TITLE = "Local Video Trimmer"

TMP_ROOT = Path(tempfile.gettempdir()) / "video_trimmer_progress"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

KEEP_HOURS = int(os.getenv("KEEP_HOURS", "24"))
MAX_TRIM_SECONDS = int(os.getenv("MAX_TRIM_SECONDS", str(60 * 60)))  # default 60 min
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

# ---------- Globals ----------
JOBS = {}  # job_id -> dict
LOCK = threading.Lock()
SEMA = threading.Semaphore(MAX_CONCURRENT_JOBS)

try:
    import yt_dlp
except Exception:
    yt_dlp = None

# ---------- Helpers ----------

def ensure_ffmpeg():
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        raise RuntimeError("ffmpeg not found in PATH")

def hms_to_seconds(txt: str) -> int:
    """Accepts 'SS', 'MM:SS', 'HH:MM:SS'."""
    if not txt:
        return 0
    t = txt.strip()
    if re.fullmatch(r"\d+", t):
        return int(t)
    parts = t.split(":")
    parts = [int(x) for x in parts]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

def parse_hms_or_hmsletters(tok: str) -> Optional[int]:
    """
    Accepts: 'SS' | 'MM:SS' | 'HH:MM:SS' | '1h30m20s' | '45s' | '5m'
    Returns seconds or None.
    """
    t = (tok or "").strip().lower()
    if not t:
        return None
    # +NN handled by caller.
    if re.fullmatch(r"\d{1,2}:\d{1,2}(:\d{1,2})?", t) or re.fullmatch(r"\d+", t):
        return hms_to_seconds(t)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", t)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s
    return None

def parse_time_token(tok):
    """
    Returns:
      - int seconds for absolute
      - {"rel": True, "val": seconds} for relative ('+..')
      - None on failure
    """
    t = (tok or "").strip().lower()
    if not t:
        return None
    if t.startswith("+"):
        val = parse_hms_or_hmsletters(t[1:])
        if val is None:
            return None
        return {"rel": True, "val": val}
    val = parse_hms_or_hmsletters(t)
    return val

def seconds_to_hms(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def now_id():
    return str(uuid.uuid4())

def clamp01(x):
    return max(0.0, min(100.0, float(x)))

def is_youtube(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        h = h.lower()
        return "youtube.com" in h or "youtu.be" in h
    except Exception:
        return False

def is_direct_media(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        p = urlparse(url).path.lower()
        return any(p.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm"))
    except Exception:
        return False

def write_job(job_id, **updates):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        j.update(updates)

def append_log(job_id: str, line: str):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        j["log"] = (j.get("log", "") + (line.rstrip() + "\n"))[-20000:]

def set_phase(job_id: str, phase: str, progress: float = None):
    if progress is None:
        write_job(job_id, phase=phase)
    else:
        write_job(job_id, phase=phase, progress=clamp01(progress))

# ---------- Background cleanup ----------

def cleanup_loop():
    while True:
        try:
            cutoff = time.time() - KEEP_HOURS * 3600
            for d in TMP_ROOT.iterdir():
                if not d.is_dir():
                    continue
                meta = d / "meta.json"
                ts = None
                if meta.exists():
                    try:
                        ts = json.loads(meta.read_text()).get("created_ts")
                    except Exception:
                        pass
                if ts and ts < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            # Keep server alive; log once per cycle
            # (we deliberately don't spam logs here)
            pass
        time.sleep(300)

threading.Thread(target=cleanup_loop, daemon=True).start()

# ---------- App ----------

app = Flask(__name__)

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{title}}</title>
<style>
:root{--bg:#0b1020;--fg:#e5e7eb;--muted:#9ca3af;--card:#151b2f;--accent:#60a5fa;--border:#23283d;--warn:#f59e0b;--danger:#ef4444}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial}
.wrap{max-width:960px;margin:26px auto;padding:0 14px}
h1{margin:0 0 6px} .muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;margin:12px 0}
label{font-weight:600;display:block;margin:8px 0 6px}
input[type=url],input[type=text]{width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;background:transparent;color:var(--fg)}
.player{aspect-ratio:16/9;background:#0a0f1c;border:1px solid var(--border);border-radius:12px;overflow:hidden}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{appearance:none;border:1px solid var(--border);background:transparent;color:var(--fg);padding:10px 14px;border-radius:10px;cursor:pointer}
.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn.warn{border-color:var(--warn);color:#fff;background:var(--warn)}
.btn.danger{border-color:var(--danger);color:#fff;background:var(--danger)}
.btn.small{padding:6px 10px}
.btn:disabled{opacity:.6;cursor:not-allowed}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:999px;padding:8px 12px}
.chip input{width:106px;border:0;background:transparent;color:var(--fg);text-align:center}
.progress{height:10px;background:#0d1224;border:1px solid var(--border);border-radius:999px;overflow:hidden}
.bar{height:100%;width:0;background:var(--accent);transition:width .25s ease}
.mono{font-family:ui-monospace,Menlo,Consolas}
.kv{display:grid;grid-template-columns:120px 1fr;gap:8px 12px;font-size:13px;margin-top:8px}
.badge{font-size:12px;border:1px solid var(--border);padding:2px 8px;border-radius:999px}
.err{color:#fecaca}
</style>
</head>
<body>
<div class="wrap">
  <h1>{{title}}</h1>
  <p class="muted">Paste a link → mark in/out or type natural times → Trim. Output is MP4 (H.264 + AAC) with faststart.</p>

  <div class="card">
    <label for="url">Video URL</label>
    <input id="url" type="url" placeholder="https://youtu.be/...  or  https://domain/video.mp4"/>

    <div class="player" style="margin-top:10px">
      <div id="ytHolder" style="width:100%;height:100%;display:none"></div>
      <video id="html5Preview" style="width:100%;height:100%;display:none" controls></video>
    </div>

    <div class="row" style="margin-top:10px">
      <button class="btn" id="btnMarkIn" type="button" onclick="mark('start')">Mark In</button>
      <button class="btn" id="btnMarkOut" type="button" onclick="mark('end')">Mark Out</button>

      <span class="chip">
        In
        <input id="start" type="text" placeholder="00:00:00"/>
        <button class="btn small" type="button" onclick="seekTo('start')" title="Seek to In">⏮</button>
      </span>

      <span class="chip">
        Out/Dur
        <input id="end" type="text" placeholder="00:00:10 or +10s"/>
        <button class="btn small" type="button" onclick="seekTo('end')" title="Seek to Out">⏭</button>
      </span>

      <div style="flex:1"></div>
      <button class="btn" type="button" onclick="previewCut()">Preview</button>
      <button id="goBtn" class="btn primary" type="button" onclick="startTrim()">Trim</button>
    </div>

    <div style="margin-top:8px">
      <input id="natural" type="text" placeholder="10:03-11:04  ·  start 1:20 end +45s  ·  1h2m–1h2m30s  ·  +30s"/>
      <div class="row" style="margin-top:6px">
        <button class="btn" type="button" onclick="applyNatural()">Apply line</button>
        <div style="flex:1"></div>
        <span id="durInfo" class="muted"></span>
      </div>
      <div id="validateMsg" class="muted" style="margin-top:4px"></div>
    </div>

    <div class="row" style="margin-top:10px">
      <button class="btn" type="button" id="clearBtn">Clear</button>
    </div>
  </div>

  <div id="jobCard" class="card" style="display:none">
    <div class="row">
      <div>Job: <span class="mono" id="jobId"></span></div>
      <div style="flex:1"></div>
      <div><span class="badge" id="jobStatus"></span></div>
    </div>
    <div class="progress" style="margin-top:8px"><div class="bar" id="bar"></div></div>
    <div class="muted" style="margin-top:6px"><span id="pct">0%</span> • <span id="phase">queued</span></div>
    <pre id="jobLog" class="mono" style="white-space:pre-wrap;max-height:240px;overflow:auto;margin-top:8px;background:#0b0f1a;padding:10px;border-radius:10px;border:1px solid var(--border)"></pre>
    <div class="kv" id="jobMeta"></div>
    <div id="jobActions" style="margin-top:10px" class="row"></div>
  </div>
</div>

<script>
let ytPlayer=null, ytReady=false, useYouTube=false, useHTML5=false, previewTimer=null;
let previewReady = false;
const urlInput=document.getElementById('url'), html5=document.getElementById('html5Preview'), ytHolder=document.getElementById('ytHolder');
const startEl=document.getElementById('start'), endEl=document.getElementById('end'), natEl=document.getElementById('natural');
const msg=document.getElementById('validateMsg'), durInfo=document.getElementById('durInfo');
const btnIn=document.getElementById('btnMarkIn'), btnOut=document.getElementById('btnMarkOut');

function secondsToHMS(sec){ sec=Math.max(0,Math.floor(sec||0)); const h=String(Math.floor(sec/3600)).padStart(2,'0'); const m=String(Math.floor((sec%3600)/60)).padStart(2,'0'); const s=String(sec%60).padStart(2,'0'); return `${h}:${m}:${s}`; }
function isYouTube(u){ try{const x=new URL(u); const h=(x.hostname||'').toLowerCase(); return h.includes('youtube.com')||h.includes('youtu.be'); }catch{return false} }
function isDirect(u){ try{const x=new URL(u); const p=(x.pathname||'').toLowerCase(); return ['.mp4','.webm','.mov','.m4v'].some(ext=>p.endsWith(ext)); }catch{return false} }

function parseFlex(t){
  t=(t||'').trim().toLowerCase();
  if(!t) return null;
  const plus=t.startsWith('+');
  const raw=plus?t.slice(1):t;
  let sec=null;
  if(/^\d{1,2}:\d{1,2}(:\d{1,2})?$/.test(raw) || /^\d+$/.test(raw)){
    const p=raw.split(':').map(Number);
    if(p.length===1) sec=p[0];
    else if(p.length===2) sec=p[0]*60+p[1];
    else sec=p[0]*3600+p[1]*60+p[2];
  } else {
    const m=raw.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/);
    if(m){ sec=(+m[1]||0)*3600 + (+m[2]||0)*60 + (+m[3]||0); }
  }
  if(sec==null || isNaN(sec)) return null;
  return plus?{rel:true,val:sec}:sec;
}

function loadPreview(){
  previewReady = false;
  const u=(urlInput.value||'').trim();
  ytHolder.style.display='none'; html5.style.display='none';
  ytPlayer=null; ytReady=false; useYouTube=false; useHTML5=false;

  const disableMark = ()=>{ btnIn.disabled=btnOut.disabled=true; };
  const enableMark  = ()=>{ btnIn.disabled=btnOut.disabled=false; };

  if(!/^https?:\/\//i.test(u)){ disableMark(); return; }

  if(isYouTube(u)){
    useYouTube=true; ytHolder.style.display='block';
    disableMark();
    if(!window.YT){
      const tag=document.createElement('script'); tag.src='https://www.youtube.com/iframe_api'; document.body.appendChild(tag);
    }else{
      onYouTubeIframeAPIReady();
    }
  } else if(isDirect(u)){
    useHTML5=true; html5.src=u; html5.style.display='block';
    disableMark();
    html5.onloadedmetadata=()=>{
      durInfo.textContent='Duration: '+secondsToHMS(html5.duration||0);
      previewReady = true;
      enableMark();
    };
  } else {
    // Non-direct URL with no preview; trimming may still work.
    durInfo.textContent='Note: Preview may not load due to CORS, trimming still works.';
    disableMark();
  }
}

window.onYouTubeIframeAPIReady=function(){
  const u=urlInput.value.trim(); const id=extractId(u); if(!id){ return; }
  ytPlayer=new YT.Player('ytHolder',{
    videoId:id,
    playerVars:{modestbranding:1,rel:0},
    events:{
      onReady:()=>{
        ytReady=true;
        previewReady = true;          // mark ready
        btnIn.disabled = btnOut.disabled = false;  // enable buttons
        try{
          const d=ytPlayer.getDuration();
          if(d) durInfo.textContent='Duration: '+secondsToHMS(d);
          // If URL had t/start param, seek there once:
          const startFrom = getStartFromURL(u);
          if (startFrom!=null) ytPlayer.seekTo(startFrom, true);
        }catch{}
      }
    }
  });
}

function getStartFromURL(u){
  try{
    const x=new URL(u);
    if (x.searchParams.has('t')) {
      const t = x.searchParams.get('t'); // like "69" or "1m9s"
      const m = t.match(/(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/);
      if (m && (m[1]||m[2]||m[3])) {
        const h=+m[1]||0, mi=+m[2]||0, s=+m[3]||0; return h*3600+mi*60+s;
      }
      const n = parseInt(t,10); if(!isNaN(n)) return n;
    }
    if (x.searchParams.has('start')) {
      const n = parseInt(x.searchParams.get('start'),10);
      if(!isNaN(n)) return n;
    }
    return null;
  }catch{ return null; }
}

function extractId(u){ try{const x=new URL(u); if(x.hostname.toLowerCase().includes('youtu.be')) return x.pathname.slice(1); return x.searchParams.get('v'); }catch{return null} }
function ensurePreviewLoaded(){
  const u=(urlInput.value||'').trim();
  if(!u) return false;
  if (!useYouTube && !useHTML5) loadPreview();
  if (useYouTube && !ytReady && window.YT && typeof YT.Player==='function') onYouTubeIframeAPIReady();
  return (useHTML5 || (useYouTube && ytReady));
}
function currentTime(){ if(useYouTube&&ytReady) return Math.floor(ytPlayer.getCurrentTime()||0); if(useHTML5) return Math.floor(html5.currentTime||0); return 0; }

function mark(which){
  const setVal = (sec)=>{
    const t=secondsToHMS(Math.floor(sec||0));
    if (which==='start') startEl.value=t; else endEl.value=t;
    updateDurationHint();
  };

  // Ensure preview exists
  if(!ensurePreviewLoaded()){
    msg.textContent='Load preview first.'; return;
  }
  if(!previewReady){
    msg.textContent='Player is getting ready… try again in a moment.'; return;
  }

  // YouTube
  if(useYouTube && ytReady && ytPlayer && typeof ytPlayer.getCurrentTime==='function'){
    let t = ytPlayer.getCurrentTime()||0;
    if (t<=0.001){
      // kick playback then sample
      try{ ytPlayer.playVideo(); }catch{}
      setTimeout(()=> setVal(ytPlayer.getCurrentTime()||0), 250);
    } else {
      setVal(t);
    }
    return;
  }

  // HTML5 <video>
  if(useHTML5 && html5){
    let t = html5.currentTime||0;
    if (t<=0.001){
      try{ html5.play(); }catch{}
      setTimeout(()=> setVal(html5.currentTime||0), 250);
    } else {
      setVal(t);
    }
    return;
  }

  msg.textContent='Preview is not available for this URL (CORS). You can still type times manually.';
}


function seekTo(which){ 
  if(!ensurePreviewLoaded()){ msg.textContent='Load preview first.'; return; }
  const v=(which==='start'?startEl.value:endEl.value).trim();
  let tok=parseFlex(v);
  let sec=null;
  if(tok && typeof tok==='object' && tok.rel){
    const sTok=parseFlex(startEl.value.trim()); const s = (typeof sTok==='number')?sTok:0;
    sec=s + tok.val;
  } else if(typeof tok==='number'){ sec=tok; }
  if(sec==null) return;
  if(useYouTube&&ytReady){ ytPlayer.seekTo(sec,true); ytPlayer.playVideo(); }
  else if(useHTML5){ html5.currentTime=sec; html5.play(); }
}

function clearPreviewTimer(){ if(previewTimer){ clearInterval(previewTimer); previewTimer=null; } }

function previewCut(){
  if(!ensurePreviewLoaded()){ msg.textContent='Load preview first.'; return; }
  clearPreviewTimer();
  const sTok=parseFlex(startEl.value.trim());
  const eRaw=(endEl.value||'').trim();
  const eTok=parseFlex(eRaw);
  if(sTok==null || eTok==null){ msg.textContent='Add valid In and Out (or +duration).'; return; }
  const s=(typeof sTok==='number')?sTok:0;
  const e=(typeof eTok==='object' && eTok.rel) ? s + eTok.val : (typeof eTok==='number'?eTok:null);
  if(e==null || e<=s){ msg.textContent='Out must be > In.'; return; }

  if(useYouTube&&ytReady){
    ytPlayer.seekTo(s,true); ytPlayer.playVideo();
    previewTimer=setInterval(()=>{ try{ if((ytPlayer.getCurrentTime()||0)>=e) ytPlayer.seekTo(s,true); }catch{} },160);
  } else if(useHTML5){
    html5.currentTime=s; html5.play();
    previewTimer=setInterval(()=>{ if((html5.currentTime||0)>=e) html5.currentTime=s; },160);
  }
}

function applyNatural(){
  const raw=(natEl.value||'').trim(); if(!raw) return;
  let s=null,e=null,rel=false;
  if(raw.includes('-')){ const [A,B]=raw.split('-'); const ta=parseFlex(A), tb=parseFlex(B); if(typeof ta==='number') s=ta; if(typeof tb==='number') e=tb; }
  if(s==null && /start\s+/i.test(raw) && /end\s+/i.test(raw)){
    const parts=raw.toLowerCase().split(/\s+/);
    const si=parts.indexOf('start'), ei=parts.indexOf('end');
    if(si>-1&&si+1<parts.length){ const v=parseFlex(parts[si+1]); if(typeof v==='number') s=v; }
    if(ei>-1&&ei+1<parts.length){
      const tok=parseFlex(parts[ei+1]);
      if(tok && typeof tok==='object' && tok.rel){ rel=true; e=tok.val; }
      else if(typeof tok==='number'){ e=tok; }
    }
  }
  if(s==null && raw.startsWith('+')){ const v=parseFlex(raw); if(v&&v.rel){ s=parseFlex(startEl.value)||0; rel=true; e=v.val; } }
  if(s==null||e==null){ msg.textContent='Could not parse. Examples: 10:03-11:04 | start 1:20 end +45s | +30s'; return; }
  startEl.value=secondsToHMS(s); endEl.value=rel?('+'+e+'s'):secondsToHMS(e);
  updateDurationHint();
}
natEl.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); applyNatural(); } });

function updateDurationHint(){
  const sTok=parseFlex(startEl.value.trim()), eTok=parseFlex(endEl.value.trim());
  if(sTok==null || eTok==null){ durInfo.textContent=''; return; }
  const s=(typeof sTok==='number')?sTok:0;
  const e=(typeof eTok==='object' && eTok.rel)? s + eTok.val : (typeof eTok==='number'?eTok:null);
  if(e==null || e<=s){ durInfo.textContent=''; return; }
  durInfo.textContent = 'Clip: '+secondsToHMS(e-s);
}

function validateInputs(){
  const u=urlInput.value.trim();
  if(!/^https?:\/\//i.test(u)){ msg.innerHTML='<span class="err">Enter a valid http(s) link.</span>'; return false; }
  if(!startEl.value.trim() || !endEl.value.trim()){ msg.innerHTML='<span class="err">Add In and Out (or a +duration).</span>'; return false; }
  msg.textContent='';
  updateDurationHint();
  return true;
}

let pollTimer=null;
function startTrim(){
  if(!validateInputs()) return;
  clearPreviewTimer();
  const payload={ url:urlInput.value.trim(), start:startEl.value.trim(), end:endEl.value.trim() };
  document.getElementById('goBtn').disabled=true;

  fetch('/api/trim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(r=>r.json()).then(d=>{
      if(!d.ok){ msg.innerHTML='<span class="err">'+(d.error||'Failed to start.')+'</span>'; document.getElementById('goBtn').disabled=false; return; }
      document.getElementById('jobCard').style.display='block';
      document.getElementById('jobId').textContent=d.jobId;
      document.getElementById('jobStatus').textContent='QUEUED';
      document.getElementById('jobLog').textContent='Queued...';
      document.getElementById('bar').style.width='5%';
      document.getElementById('jobMeta').innerHTML='';
      const actions=document.getElementById('jobActions');
      actions.innerHTML='';
      const cancelBtn=document.createElement('button'); cancelBtn.className='btn danger'; cancelBtn.textContent='Cancel'; cancelBtn.onclick=()=>cancelJob(d.jobId);
      actions.appendChild(cancelBtn);
      pollTimer=setInterval(()=>pollStatus(d.jobId), 800);
    }).catch(e=>{ msg.innerHTML='<span class="err">Failed to start: '+e+'</span>'; document.getElementById('goBtn').disabled=false; });
}

function cancelJob(id){
  fetch('/api/cancel/'+id,{method:'POST'}).then(()=>{}).catch(()=>{});
}

function pollStatus(id){
  fetch('/api/status/'+id).then(r=>r.json()).then(s=>{
    const bar=document.getElementById('bar'), pct=document.getElementById('pct'), phase=document.getElementById('phase'),
          logEl=document.getElementById('jobLog'), actions=document.getElementById('jobActions'), meta=document.getElementById('jobMeta');
    document.getElementById('jobStatus').textContent=s.status||s.phase||'';
    if(bar) bar.style.width=((s.progress||0))+'%';
    if(pct) pct.textContent=(s.progress||0)+'%';
    if(phase) phase.textContent=s.phase||s.status||'';
    if(logEl && s.log) logEl.textContent=s.log;
    if(meta){ meta.innerHTML = '';
      if(s.meta){ for(const [k,v] of Object.entries(s.meta)){ const kd=document.createElement('div'); kd.textContent=k; const vd=document.createElement('div'); vd.textContent=v; meta.appendChild(kd); meta.appendChild(vd);} }
    }
    if(s.status==='COMPLETE'){ clearInterval(pollTimer); document.getElementById('goBtn').disabled=false;
      actions.innerHTML='';
      if(s.download){
        const a=document.createElement('a'); a.className='btn primary'; a.href=s.download; a.textContent='⬇ Download clip'; actions.appendChild(a);
      }
    }
    if(s.status==='FAILED' || s.status==='CANCELLED'){ clearInterval(pollTimer); document.getElementById('goBtn').disabled=false; }
  }).catch(()=>{});
}

urlInput.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); loadPreview(); }});
document.getElementById('clearBtn').addEventListener('click', ()=>{
  urlInput.value=''; startEl.value=''; endEl.value=''; natEl.value='';
  ytHolder.style.display='none'; html5.style.display='none'; msg.textContent=''; durInfo.textContent='';
  clearPreviewTimer();
  document.getElementById('jobCard').style.display='none';
});
urlInput.addEventListener('change', loadPreview);
let _lpTimer = null;
function _debouncedLoad(){ clearTimeout(_lpTimer); _lpTimer = setTimeout(loadPreview, 250); }
urlInput.addEventListener('input', _debouncedLoad);
urlInput.addEventListener('blur',  _debouncedLoad);
window.addEventListener('DOMContentLoaded', loadPreview);
</script>
</body>
</html>
"""

# ---------- Routes ----------

@app.get("/")
def index():
    return Response(INDEX_HTML.replace("{{title}}", APP_TITLE), mimetype="text/html")

@app.get("/downloads/<job_id>/<path:filename>")
def downloads(job_id, filename):
    d = TMP_ROOT / job_id
    if not d.exists():
        return "Not found", 404
    return send_from_directory(str(d), filename, as_attachment=True)

@app.post("/api/trim")
def api_trim():
    try:
        data = request.get_json(force=True, silent=True) or {}
        url = (data.get("url") or "").strip()
        start_raw = (data.get("start") or "").strip()
        end_raw = (data.get("end") or "").strip()

        if not url.lower().startswith(("http://", "https://")):
            return jsonify(ok=False, error="Invalid URL")

        ps = parse_time_token(start_raw)
        pe = parse_time_token(end_raw)
        if ps is None or pe is None:
            return jsonify(ok=False, error="Could not parse start/end")

        if isinstance(ps, dict) and ps.get("rel"):
            return jsonify(ok=False, error="Start must be absolute (not +duration)")
        start_sec = int(ps)

        if isinstance(pe, dict) and pe.get("rel"):
            dur_sec = int(pe["val"])
            end_sec = start_sec + dur_sec
            is_rel = True
        else:
            end_sec = int(pe)
            dur_sec = end_sec - start_sec
            is_rel = False

        if dur_sec <= 0:
            return jsonify(ok=False, error="End must be after start")
        if dur_sec > MAX_TRIM_SECONDS:
            return jsonify(ok=False, error=f"Trim length too large (> {MAX_TRIM_SECONDS//60} min)")

        job_id = now_id()
        job_dir = TMP_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "meta.json").write_text(json.dumps({"created_ts": time.time()}, ensure_ascii=False))

        with LOCK:
            JOBS[job_id] = {
                "id": job_id, "url": url, "start": start_sec, "end": end_sec,
                "duration": dur_sec, "rel": is_rel, "status": "QUEUED",
                "log": "Queued...\n", "output": None, "phase": "queued",
                "progress": 0.0, "proc": None, "cancel": False, "meta": {}
            }

        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        return jsonify(ok=True, jobId=job_id)
    except Exception as e:
        return jsonify(ok=False, error=f"Server error: {e}"), 500

@app.get("/api/status/<job_id>")
def api_status(job_id):
    with LOCK:
        j = JOBS.get(job_id)
    if not j:
        return jsonify(ok=False), 404
    resp = {
        "ok": True,
        "status": j["status"],
        "phase": j.get("phase", "queued"),
        "progress": round(float(j.get("progress", 0)), 1),
        "log": j.get("log", ""),
        "meta": j.get("meta", {}),
    }
    if j["status"] == "COMPLETE" and j.get("output"):
        resp["download"] = f"/downloads/{j['id']}/{Path(j['output']).name}"
    return jsonify(resp)

@app.post("/api/cancel/<job_id>")
def api_cancel(job_id):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify(ok=False), 404
        j["cancel"] = True
        proc = j.get("proc")
    try:
        if proc and proc.poll() is None:
            proc.terminate()
            # give a moment, then kill if needed
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    except Exception:
        pass
    write_job(job_id, status="CANCELLED", phase="cancelled")
    append_log(job_id, "Job cancelled by user.")
    return jsonify(ok=True)

@app.get("/api/ping")
def api_ping():
    return jsonify(ok=True, ts=time.time())

# ---------- Worker ----------

def run_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return

    job_dir = TMP_ROOT / job_id

    def fail(msg: str):
        append_log(job_id, msg)
        write_job(job_id, status="FAILED", phase="failed")

    def cancelled() -> bool:
        with LOCK:
            j = JOBS.get(job_id)
            return bool(j and j.get("cancel"))

    try:
        ensure_ffmpeg()
    except Exception as e:
        fail(str(e))
        return

    # Do not exceed concurrency
    if not SEMA.acquire(timeout=1):
        fail("Server busy; please try again shortly.")
        return

    try:
        # ---------- Download (when using yt_dlp) ----------
        url = job["url"]
        infile = None
        used_ytdlp = False

        if yt_dlp is None and is_youtube(url):
            fail("yt_dlp not installed (pip install yt-dlp)")
            return

        append_log(job_id, "Preparing download...")
        write_job(job_id, status="RUNNING")
        set_phase(job_id, "downloading", 0.0)

        if is_youtube(url) or (yt_dlp and not is_direct_media(url)):
            used_ytdlp = True
            outtmpl = str(job_dir / "input.%(ext)s")
            ydl_opts = {
                "outtmpl": outtmpl,
                "format": "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "retries": 5,
                "fragment_retries": 5,
                "http_headers": {"User-Agent": "Mozilla/5.0"},
            }
            # Range download for efficiency (always when absolute cut)
            if not job["rel"]:
                ydl_opts["download_sections"] = {"*": [{"start_time": job["start"], "end_time": job["end"]}]}
                append_log(job_id, f"Partial download: {job['start']}s → {job['end']}s")

            def _yt_hook(d):
                try:
                    if d.get('status') == 'downloading':
                        p = d.get('_percent_str', '').strip().replace('%', '')
                        set_phase(job_id, "downloading", float(p or 0))
                    elif d.get('status') == 'finished':
                        set_phase(job_id, "downloading", 100.0)
                except Exception:
                    pass
            ydl_opts["progress_hooks"] = [_yt_hook]

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
            except Exception as e:
                fail(f"Download failed: {e}")
                return

            # locate input file (sections may add suffixes; accept any 'input*.mp4|webm|mov|m4v')
            cands = sorted(list(job_dir.glob("input*.mp4"))) or \
                    sorted(list(job_dir.glob("input*.webm"))) or \
                    sorted(list(job_dir.glob("input*.mov"))) or \
                    sorted(list(job_dir.glob("input*.m4v")))
            if not cands:
                fail("Downloaded file not found")
                return
            infile = str(cands[0])
        else:
            # Direct HTTP media: let ffmpeg read remote URL during trim (single phase)
            infile = url
            used_ytdlp = False
            append_log(job_id, "Direct media URL – will trim directly from source (no separate download).")

        if cancelled():
            write_job(job_id, status="CANCELLED", phase="cancelled")
            append_log(job_id, "Cancelled before trimming.")
            return

        # ---------- Trim ----------
        set_phase(job_id, "trimming", 0.0)
        outfile = str(job_dir / f"trimmed_{job_id}.mp4")
        start_ts = seconds_to_hms(job["start"])
        dur_ts = seconds_to_hms(job["duration"])

        # ffmpeg command: H.264 + AAC, +faststart
        ff_cmd = [
            "ffmpeg", "-y",
            "-ss", start_ts, "-i", infile, "-t", dur_ts,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart",
            "-fflags", "+genpts", "-avoid_negative_ts", "make_zero", "-shortest",
            outfile
        ]
        append_log(job_id, "Running: " + " ".join(ff_cmd))

        # Progress parsing: read ffmpeg stderr lines; compute from 'time=' as encoded duration (relative to 0)
        total_trim = max(1, job["duration"])
        try:
            proc = subprocess.Popen(
                ff_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(job_dir),
            )
            write_job(job_id, proc=proc)
            for line in proc.stdout:
                if cancelled():
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                    write_job(job_id, status="CANCELLED", phase="cancelled")
                    append_log(job_id, "Cancelled during trimming.")
                    return

                if "time=" in line:
                    # typical: "time=00:00:05.12"
                    m = re.search(r'time=(\d+):(\d+):(\d+)\.(\d+)', line)
                    if m:
                        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        cur = hh * 3600 + mm * 60 + ss  # already relative due to -ss before -i
                        pct = (cur / total_trim) * 100.0
                        set_phase(job_id, "trimming", pct)
                else:
                    append_log(job_id, line.rstrip())
            rc = proc.wait(timeout=60 * 60)
        except Exception as e:
            fail(f"ffmpeg failed: {e}")
            return
        finally:
            write_job(job_id, proc=None)

        if rc != 0:
            fail(f"ffmpeg exit code {rc}")
            return

        # Success
        meta = {
            "Clip length": f"{seconds_to_hms(job['duration'])}",
            "Start @": seconds_to_hms(job["start"]),
            "End @": seconds_to_hms(job["end"]),
            "Saved": Path(outfile).name,
        }
        write_job(job_id, status="COMPLETE", phase="complete", progress=100.0, output=outfile, meta=meta)
        append_log(job_id, "Done.")

    finally:
        try:
            SEMA.release()
        except Exception:
            pass

# ---------- Signals & Main ----------

def _exit(*_):
    os._exit(0)

signal.signal(signal.SIGINT, _exit)
signal.signal(signal.SIGTERM, _exit)

if __name__ == "__main__":
    print(f"Workdir: {TMP_ROOT}")
    app.run(host=HOST, port=PORT, threaded=True)
