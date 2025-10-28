#!/usr/bin/env python3

import os
import subprocess
import threading
import time
import queue
import socket
from flask import Flask, Response, request, jsonify, render_template_string
import tkinter as tk
from tkinter import ttk

app = Flask(__name__)

# ---------- CONFIGURAÇÃO ----------
PORT = 8080
CHUNK_SIZE = 1024
FFMPEG_BIN = "ffmpeg"
MUSIC_FOLDER = r"C:\Users\filip\OneDrive\Desktop\codigos\pessoal\outros\music"
ALLOWED_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'}
# -----------------------------------

state_lock = threading.Lock()
playlist = []  # lista de dicts: { 'id': '001', 'path': 'C:\...','name':'file.mp3' }
index = 0
paused = True
loop_mode = "none"
broadcaster_thread = None
broadcaster_stop = threading.Event()
clients = set()
skip_event = threading.Event()
action_pending = None
action_pending_index = None  # usado para saltos diretos


def log(msg):
    print(f"[radio] {msg}", flush=True)


def scan_playlist():
    """Atualiza a lista de músicas; cada arquivo recebe um ID interno de 3 dígitos.
    Retorna o número de arquivos encontrados."""
    global playlist
    found = []
    try:
        if not os.path.exists(MUSIC_FOLDER):
            log(f"Pasta não existe: {MUSIC_FOLDER}")
            playlist = []
            return 0
        for root, _, files in os.walk(MUSIC_FOLDER):
            for fname in sorted(files):
                _, ext = os.path.splitext(fname)
                if ext.lower() in ALLOWED_EXT:
                    path = os.path.join(root, fname)
                    if os.path.isfile(path):
                        found.append(path)
        # montar playlist com IDs de 3 dígitos
        new_pl = []
        for i, p in enumerate(found, start=1):
            id_str = f"{i:03d}"
            new_pl.append({ 'id': id_str, 'path': p, 'name': os.path.basename(p) })
        with state_lock:
            playlist = new_pl
            # se índice atual for maior que o novo tamanho, ajustar
            global index
            if index >= len(playlist):
                index = max(0, len(playlist)-1)
        log(f"scan_playlist: encontrou {len(playlist)} arquivo(s).")
        for item in playlist:
            log(f"  {item['id']}: {item['path']}")
        return len(playlist)
    except Exception as e:
        log(f"Erro ao escanear pasta: {e}")
        playlist = []
        return 0


def start_broadcaster():
    global broadcaster_thread, broadcaster_stop
    if broadcaster_thread and broadcaster_thread.is_alive():
        return
    broadcaster_stop.clear()
    broadcaster_thread = threading.Thread(target=broadcaster_loop, daemon=True)
    broadcaster_thread.start()
    log("broadcaster iniciado.")


def broadcaster_loop():
    global index, paused, loop_mode, action_pending, action_pending_index
    manual_advance = False  # flag para controlar skip manual
    current_proc = None

    while not broadcaster_stop.is_set():
        with state_lock:
            has_playlist = bool(playlist)
        if not has_playlist:
            time.sleep(0.5)
            continue

        # aguarda se estiver pausado
        while True:
            with state_lock:
                is_paused = paused
            if not is_paused:
                break
            time.sleep(0.05)
            if broadcaster_stop.is_set():
                return

        with state_lock:
            cur_item = playlist[index]
            cur_path = cur_item['path']

        if not os.path.isfile(cur_path):
            with state_lock:
                try:
                    playlist.pop(index)
                except Exception:
                    pass
                if index >= len(playlist):
                    index = max(0, len(playlist)-1)
            continue

        # Garante que não haja outro processo ativo
        if current_proc is not None:
            try:
                if current_proc.poll() is None:
                    current_proc.kill()
                    current_proc.wait(timeout=0.2)
            except Exception:
                pass
            current_proc = None

        cmd = [FFMPEG_BIN, "-re", "-i", cur_path, "-vn", "-f", "mp3", "-ab", "192k", "pipe:1", "-loglevel", "error"]
        log(f"Tocando: {cur_item['id']} - {cur_path}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        current_proc = proc

        try:
            while True:
                if broadcaster_stop.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

                # skip/prev or jump via endpoint
                if skip_event.is_set():
                    with state_lock:
                        if action_pending == "next":
                            index = (index + 1) % len(playlist)
                        elif action_pending == "prev":
                            index = (index - 1 + len(playlist)) % len(playlist)
                        elif action_pending == "set_index" and action_pending_index is not None:
                            # salto direto
                            if 0 <= action_pending_index < len(playlist):
                                index = action_pending_index
                        action_pending = None
                        action_pending_index = None
                        manual_advance = True
                    # mata o processo atual e espera ele terminar
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass

                    # limpa os buffers das filas dos clientes para evitar sobreposição de áudio
                    for q in list(clients):
                        try:
                            while True:
                                q.get_nowait()
                        except Exception:
                            pass

                    skip_event.clear()
                    current_proc = None
                    break

                chunk = proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break

                # distribuir para clientes
                dead = []
                for q in list(clients):
                    try:
                        q.put(chunk, timeout=0.5)
                    except Exception:
                        dead.append(q)
                for d in dead:
                    clients.discard(d)

                # pausa: não mata o processo, apenas espera antes de enviar chunks
                while paused and not broadcaster_stop.is_set() and not skip_event.is_set():
                    time.sleep(0.05)

        finally:
            # garante que o processo foi finalizado
            try:
                if proc and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.2)
            except Exception:
                pass
            current_proc = None

        # próxima faixa segundo loop_mode
        with state_lock:
            if not playlist:
                continue

            if manual_advance:
                manual_advance = False
                continue

            if loop_mode == "one":
                pass
            elif loop_mode == "all":
                index = (index + 1) % len(playlist)
            else:
                if index + 1 < len(playlist):
                    index += 1
                else:
                    paused = True
        time.sleep(0.05)


def stream_generator(client_q):
    try:
        while True:
            try:
                chunk = client_q.get(timeout=2)
                yield chunk
            except queue.Empty:
                continue
    finally:
        try:
            clients.remove(client_q)
        except Exception:
            pass
        print(f"[radio] cliente desconectado. clientes atuais: {len(clients)}", flush=True)


@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=512)
    clients.add(q)
    print(f"[radio] cliente conectado. clientes atuais: {len(clients)}", flush=True)
    start_broadcaster()
    try:
        return Response(stream_generator(q), mimetype='audio/mpeg')
    finally:
        print(f"[radio] stream endpoint returning (client disconnected?)", flush=True)


@app.route("/play", methods=["POST"])
def play():
    global paused
    with state_lock:
        if not playlist:
            return jsonify({"error":"Playlist vazia"}), 400
        paused = False
    start_broadcaster()
    return jsonify({"status":"playing"})


@app.route("/pause", methods=["POST"])
def pause():
    global paused
    with state_lock:
        paused = True
    return jsonify({"status":"paused"})


@app.route("/next", methods=["POST"])
def nxt():
    global action_pending, paused
    with state_lock:
        if not playlist:
            return jsonify({"error":"Playlist vazia"}), 400
        action_pending = "next"
        skip_event.set()
        paused = False
    return jsonify({"status":"skipped", "action":"next"})


@app.route("/prev", methods=["POST"])
def prev():
    global action_pending, paused
    with state_lock:
        if not playlist:
            return jsonify({"error":"Playlist vazia"}), 400
        action_pending = "prev"
        skip_event.set()
        paused = False
    return jsonify({"status":"previous", "action":"prev"})


@app.route("/select", methods=["POST"])
def select_by_id():
    """Seleciona diretamente uma faixa pelo ID interno (ex: '001').
    Corpo JSON: { "id": "005" }
    """
    global action_pending, action_pending_index, paused
    data = request.get_json(force=True) or {}
    id_req = (data.get('id') or '').strip()
    if not id_req:
        return jsonify({"error": "id is required"}), 400

    with state_lock:
        # procura índice correspondente
        found_idx = None
        for i, item in enumerate(playlist):
            if item['id'] == id_req:
                found_idx = i
                break
        if found_idx is None:
            return jsonify({"error": "id not found"}), 404
        action_pending = "set_index"
        action_pending_index = found_idx
        skip_event.set()
        paused = False

    return jsonify({"status": "ok", "selected": playlist[found_idx]['id']})


@app.route("/loop", methods=["POST"])
def set_loop():
    global loop_mode
    data = request.json or {}
    mode = data.get("mode")
    if mode not in ("none","one","all"):
        return jsonify({"error":"mode deve ser 'none','one' ou 'all'"}), 400
    with state_lock:
        loop_mode = mode
    return jsonify({"loop": loop_mode})


@app.route("/status")
def status():
    with state_lock:
        cur = playlist[index] if playlist else None
        info = {
            # playlist como lista de objetos {id,name}
            "playlist": [{ 'id': p['id'], 'name': p['name'] } for p in playlist],
            "index": index,
            "current": { 'id': cur['id'], 'name': cur['name'] } if cur else None,
            "paused": paused,
            "loop": loop_mode,
            "clients": len(clients)
        }
    return jsonify(info)


# Frontend (igual ao original, mas ajustado para trabalhar com IDs)
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Estação Rádio - Player</title>
  <style>
    :root{
      --bg:#0f1115; --card:#121317; --muted:#9aa4b2; --accent:#6ee7b7; --accent-2:#60a5fa; --glass: rgba(255,255,255,0.03);
      --surface:#0b0c0f; --danger:#ff6b6b; --radius:12px; --shadow: 0 6px 24px rgba(2,6,23,0.6);
      font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
    }
    *{box-sizing:border-box}
    html,body{height:100%;margin:0;background:linear-gradient(180deg,var(--bg),#070708);color:#e6eef6}
    .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:28px}
    .card{width:980px;max-width:96%;background:linear-gradient(180deg,var(--card),#0b0c0f);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px;display:grid;grid-template-columns:1fr;gap:18px}

    .left {padding:8px 6px}
    h1{margin:0 0 6px 0;font-size:20px;letter-spacing:0.4px}
    .meta{color:var(--muted);font-size:13px;margin-bottom:12px}

    /* player */
    .player-box{background:linear-gradient(180deg, rgba(255,255,255,0.02), transparent);padding:14px;border-radius:10px;border:1px solid rgba(255,255,255,0.03);display:flex;flex-direction:column;gap:10px}
    audio{width:100%;outline:none;border-radius:8px}

    .controls{display:flex;align-items:center;gap:8px}
    .btn{background:var(--glass);border:1px solid rgba(255,255,255,0.04);padding:8px 10px;border-radius:10px;color:var(--accent);cursor:pointer;font-weight:600}
    .btn.small{padding:6px 8px;font-size:13px}
    .btn.secondary{background:transparent;color:var(--muted);border:1px solid rgba(255,255,255,0.03)}
    .right{margin-left:auto;display:flex;align-items:center;gap:8px}
    .small{background:transparent;border:1px solid rgba(255,255,255,0.04);padding:7px 8px;border-radius:8px;color:var(--muted)}

    .loader{display:inline-block;vertical-align:middle;width:12px;height:12px;border-radius:50%;opacity:0.9;border:2px solid transparent;border-top-color:var(--accent);animation:spin 900ms linear infinite}
    .hidden{display:none}
    @keyframes spin{to{transform:rotate(360deg)}}

    .playlist-row{display:flex;gap:8px;align-items:center;margin-top:10px}
    select{background:transparent;border:1px solid rgba(255,255,255,0.04);padding:8px;border-radius:8px;color:var(--muted)}

    .status{margin-top:12px;color:var(--muted);font-size:13px}

    /* playlist abaixo */
    .playlist-panel{display:flex;gap:12px;align-items:flex-start}
    .card-panel{background:linear-gradient(180deg, rgba(255,255,255,0.02), transparent);padding:12px;border-radius:12px;border:1px solid rgba(255,255,255,0.03);width:100%}
    #pl{list-style:none;margin:0;padding:8px;max-height:360px;overflow:auto}
    #pl li{padding:10px;border-radius:8px;margin-bottom:6px;background:transparent;border:1px solid rgba(255,255,255,0.02);display:flex;justify-content:space-between;align-items:center;cursor:pointer}
    #pl li:hover{background:rgba(255,255,255,0.02)}
    #pl li.active{background:linear-gradient(90deg, rgba(102,255,203,0.06), rgba(96,165,250,0.04));border:1px solid rgba(110,231,183,0.12)}
    .id{font-family:monospace;color:var(--accent-2);margin-right:8px}
    .fname{flex:1;color:#d7e6f6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

    .meta-row{display:flex;justify-content:space-between;align-items:center;margin-top:10px;color:var(--muted);font-size:13px}

    .foot{grid-column:1 / -1;margin-top:10px;color:var(--muted);font-size:12px;text-align:right}

    @media (max-width:880px){.card{grid-template-columns:1fr;}.right{margin-left:0}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="left">
        <h1>Estação Rádio</h1>
        <div class="meta">Streaming local — abra <code>/stream</code> se quiser receber áudio bruto.</div>

        <div class="player-box">
          <audio id="player" controls autoplay>
            <source src="/stream" type="audio/mpeg">
            Seu navegador não suporta reprodução de áudio.
          </audio>

          <div class="controls">
            <button id="btnPrev" class="btn">⏮ Prev <span id="loaderPrev" class="loader hidden"></span></button>
            <button id="btnPlay" class="btn">▶ Play <span id="loaderPlay" class="loader hidden"></span></button>
            <button id="btnPause" class="btn">⏸ Pause <span id="loaderPause" class="loader hidden"></span></button>
            <button id="btnNext" class="btn">⏭ Next <span id="loaderNext" class="loader hidden"></span></button>

            <div class="right">
              <select id="loop" class="small">
                <option value="none">Sem loop</option>
                <option value="all">Loop fila</option>
                <option value="one">Loop 1 música</option>
              </select>
              <button id="btnRescan" class="btn secondary small">🔁 Rescan</button>
            </div>
          </div>

          <div id="statusText" class="status">Carregando estado...</div>
        </div>

        <div class="playlist-panel">
          <div class="card-panel">
            <h3 style="margin:0 0 8px 0;color:#eaf6f0">Playlist</h3>
            <ul id="pl" aria-label="Playlist"></ul>
            <div class="meta-row"><div>Clientes conectados: <span id="clientsCount">0</span></div><div><button id="btnScrollToCurrent" class="btn small">Ir para atual</button></div></div>
          </div>
        </div>

      </div>

      <div class="foot">Feito para uso pessoal — tema escuro ativo</div>
    </div>
  </div>

<script>
  // novo comportamento: playlist clicável — cada item seleciona a música correspondente
  let lastStatus = { playlist: [], index: 0, paused: true, loop: 'none' };
  let player = null;
  let suppressRefreshUntil = 0;

  function showLoader(id, show){
    const el = document.getElementById(id);
    if(!el) return;
    if(show) el.classList.remove('hidden'); else el.classList.add('hidden');
  }

  async function callEndpoint(path, opts={}){
    try{
      const res = await fetch(path, Object.assign({ method: 'POST' }, opts));
      return res;
    }catch(e){ console.error('Erro ao chamar endpoint', path, e); throw e; }
  }

  async function doControlWithLoader(path, loaderId){
    showLoader(loaderId, true);
    try{
      const p = callEndpoint(path);
      return await p;
    }finally{
      setTimeout(()=>showLoader(loaderId, false), 150);
      setTimeout(refreshStatus, 150);
    }
  }

  async function doControl(path){
    if(!player) player = document.getElementById('player');
    if (path === '/play'){
      lastStatus.paused = false; renderFromLastStatus(); await doControlWithLoader('/play','loaderPlay');
      try{ player.play().catch(()=>{}); }catch(e){}
    } else if (path === '/pause'){
      lastStatus.paused = true; renderFromLastStatus(); await doControlWithLoader('/pause','loaderPause');
      try{ player.pause(); }catch(e){}
    } else if (path === '/next'){
      if(lastStatus.playlist.length>0) lastStatus.index = (lastStatus.index+1)%lastStatus.playlist.length;
      renderFromLastStatus(); await doControlWithLoader('/next','loaderNext');
      try{ player.play().catch(()=>{}); }catch(e){}
    } else if (path === '/prev'){
      if(lastStatus.playlist.length>0) lastStatus.index = (lastStatus.index-1+lastStatus.playlist.length)%lastStatus.playlist.length;
      renderFromLastStatus(); await doControlWithLoader('/prev','loaderPrev');
      try{ player.play().catch(()=>{}); }catch(e){}
    }
    if (player) {
        const currentSrc = '/stream?t=' + Date.now();
        player.src = currentSrc;
        player.load();
        player.play().catch(()=>{});
    }
  }

  async function setLoop(){
    const mode = document.getElementById('loop').value;
    try{
      await fetch('/loop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ mode: mode }) });
      lastStatus.loop = mode; renderFromLastStatus();
    }catch(e){ console.warn('Erro setLoop', e); }
    setTimeout(refreshStatus, 150);
  }

  async function rescan(){
    showLoader('loaderRescan', true);
    try{ await fetch('/rescan', { method: 'POST' }); }catch(e){}
    showLoader('loaderRescan', false);
    await refreshStatus();
  }

  function renderFromLastStatus(){
    const pl = document.getElementById('pl');
    pl.innerHTML = '';
    lastStatus.playlist.forEach((p,i)=> {
      const li = document.createElement('li');
      li.setAttribute('data-id', p.id);
      li.setAttribute('role','button');
      li.innerHTML = `<span style="display:flex;align-items:center"><span class=\"id\">${p.id}</span><span class=\"fname\">${p.name}</span></span>`;
      li.addEventListener('click', ()=> selectTrack(p.id, i, li));
      if (i===lastStatus.index) li.classList.add('active');
      pl.appendChild(li);
    });

    const statusEl = document.getElementById('statusText');
    statusEl.innerText = (lastStatus.paused ? '⏸️ Pausado' : '▶️ Tocando') + ' | Loop: ' + (lastStatus.loop || 'none') + ' | Faixa: ' + (lastStatus.playlist[lastStatus.index] ? lastStatus.playlist[lastStatus.index].name : '—');

    document.getElementById('clientsCount').innerText = (lastStatus.clients || 0);
  }

  async function refreshStatus(){
    try{
      const r = await fetch('/status'); if(!r.ok) return;
      const j = await r.json();
      const now = Date.now();
      lastStatus.playlist = j.playlist || [];
      if (now >= suppressRefreshUntil){
        lastStatus.index = (typeof j.index === 'number') ? j.index : 0;
      }
      lastStatus.paused = !!j.paused;
      lastStatus.loop = j.loop || j.loop_mode || 'none';
      lastStatus.clients = j.clients || 0;
      renderFromLastStatus();
    }catch(e){ console.error('refreshStatus', e); }
  }

  // seleciona faixa pelo ID (chamada ao clicar em um item da playlist)
  async function selectTrack(desiredId, desiredIndex, liElement){
    if(!desiredId) return;
    showLoader('loaderSelect', true);
    // otimistic UI
    lastStatus.index = desiredIndex;
    renderFromLastStatus();
    suppressRefreshUntil = Date.now() + 2500;

    // força o navegador a recarregar o stream
    if (player) {
        const currentSrc = '/stream?t=' + Date.now();
        player.pause();
        player.src = currentSrc;
        player.load();
        player.play().catch(() => {});
    }

    try{
      await fetch('/select', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ id: desiredId }) });
    }catch(e){ console.warn('Erro ao solicitar seleção direta', e); }

    try{ await callEndpoint('/play'); }catch(e){}

    const timeoutAt = Date.now()+3500;
    while(Date.now()<timeoutAt){
      await refreshStatus();
      if(lastStatus.playlist[lastStatus.index] && lastStatus.playlist[lastStatus.index].id === desiredId) break;
      await new Promise(r=>setTimeout(r, 150));
    }

    showLoader('loaderSelect', false);
    renderFromLastStatus();
    // garante que o item selecionado fique visível
    try{ const pl = document.getElementById('pl'); const itm = pl.children[ lastStatus.index ]; if(itm) itm.scrollIntoView({behavior:'smooth', block:'center'}); }catch(e){}
  }

  document.addEventListener('DOMContentLoaded', ()=>{
    player = document.getElementById('player');
    document.getElementById('btnPlay').addEventListener('click', ()=>doControl('/play'));
    document.getElementById('btnPause').addEventListener('click', ()=>doControl('/pause'));
    document.getElementById('btnNext').addEventListener('click', ()=>doControl('/next'));
    document.getElementById('btnPrev').addEventListener('click', ()=>doControl('/prev'));
    document.getElementById('btnRescan').addEventListener('click', ()=>rescan());
    document.getElementById('loop').addEventListener('change', ()=>setLoop());
    document.getElementById('btnScrollToCurrent').addEventListener('click', ()=>{
      const pl = document.getElementById('pl');
      const items = pl.children;
      const idx = lastStatus.index;
      if(items && items[idx]){
        items[idx].scrollIntoView({behavior:'smooth', block:'center'});
      }
    });

    refreshStatus();
    setInterval(refreshStatus, 900);
    (async ()=>{
      try{
        await refreshStatus();
        if(lastStatus.paused){
          doControl('/play').catch(()=>{});
        }
        try{ player.play().catch(()=>{}); }catch(e){}
      }catch(e){}
    })();

    player.addEventListener('ended', () => {
        console.log('Stream ended — reconnecting...');
        setTimeout(() => {
            player.src = '/stream?t=' + Date.now(); // força nova conexão
            player.load();
            player.play().catch(()=>{});
        }, 500);
    });

    player.addEventListener('error', () => {
        console.warn('Stream error — reconnecting...');
        setTimeout(() => {
            player.src = '/stream?t=' + Date.now();
            player.load();
            player.play().catch(()=>{});
        }, 500);
    });
  });
</script>
</body>
</html>
"""

@app.route("/")
def index_page():
    return render_template_string(INDEX_HTML)


@app.route("/files")
def list_files():
    scan_playlist()
    return jsonify([{ 'id': p['id'], 'name': p['name'] } for p in playlist])


@app.route("/rescan", methods=["GET","POST"])
def rescan():
    n = scan_playlist()
    return jsonify({"status":"scanned", "count": n})


@app.route("/debug")
def debug():
    exists = os.path.exists(MUSIC_FOLDER)
    total = 0
    try:
        total = sum(1 for _ in os.listdir(MUSIC_FOLDER)) if exists else 0
    except Exception as e:
        total = f"error: {e}"
    return jsonify({
        "MUSIC_FOLDER": MUSIC_FOLDER,
        "exists": exists,
        "entries_in_folder": total,
        "allowed_ext": list(ALLOWED_EXT)
    })


@app.route("/control", methods=["POST"])
def control():
    global paused, index, loop_mode, action_pending, action_pending_index
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    action = (data.get("action") or "").lower()

    with state_lock:
        if action == "play":
            paused = False
        elif action == "pause":
            paused = True
        elif action == "toggle":
            paused = not paused
        elif action in ("next", "prev"):
            if playlist:
                action_pending = action
                skip_event.set()
                paused = False
        elif action == "loop_one":
            loop_mode = "one"
        elif action == "loop_all":
            loop_mode = "all"
        elif action == "loop_off":
            loop_mode = "none"
        else:
            return jsonify({"error": "Unknown action"}), 400

        current = playlist[index] if playlist else None

    log(f"Ação recebida: {action}, faixa atual: {current}")
    return jsonify({
        "status": "ok",
        "paused": paused,
        "loop_mode": loop_mode,
        "current": current
    })


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

# Tkinter controls atualizados para exibir ID
def start_tkinter_controls():
    """Janela Tkinter com tema escuro, playlist rolável e nomes de músicas em fonte maior."""
    def set_play():
        with state_lock:
            globals()['paused'] = False
        stop_loading()

    def set_pause():
        with state_lock:
            globals()['paused'] = True
        stop_loading()

    def do_next():
        with state_lock:
            if playlist:
                globals()['action_pending'] = 'next'
                skip_event.set()
                globals()['paused'] = False
        start_loading()

    def do_prev():
        with state_lock:
            if playlist:
                globals()['action_pending'] = 'prev'
                skip_event.set()
                globals()['paused'] = False
        start_loading()

    def set_loop(mode):
        with state_lock:
            if mode == 'one':
                globals()['loop_mode'] = 'one'
            elif mode == 'all':
                globals()['loop_mode'] = 'all'
            else:
                globals()['loop_mode'] = 'none'

    def do_rescan():
        start_loading('Rescan...')
        scan_playlist()
        stop_loading()
        update_ui()

    def select_from_list(evt=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        with state_lock:
            if idx < 0 or idx >= len(playlist):
                return
            globals()['action_pending'] = 'set_index'
            globals()['action_pending_index'] = idx
            skip_event.set()
            globals()['paused'] = False
        start_loading('Trocando...')

    def start_loading(text='Carregando...'):
        loader_var.set(text)
        loader_label.pack(side='right')

    def stop_loading():
        loader_var.set('')
        loader_label.pack_forget()

    def update_ui():
        with state_lock:
            pl_copy = list(playlist)
            cur_index = index if playlist else 0
            cur_paused = globals().get('paused', True)
            lm = globals().get('loop_mode', 'none')
            clients_count = len(clients)

        try:
            view_pos = lb.yview()[0]
        except Exception:
            view_pos = 0.0

        lb.delete(0, tk.END)
        for i, p in enumerate(pl_copy):
            display = f"{p['id']} - {p['name']}"
            lb.insert(tk.END, display)
            if i == cur_index:
                lb.itemconfig(i, fg='#bfeee0')

        try:
            if pl_copy:
                lb.selection_clear(0, tk.END)
                lb.selection_set(cur_index)
                lb.yview_moveto(view_pos)
        except Exception:
            pass

        status_text = ('⏸️ Pausado' if cur_paused else '▶️ Tocando') + f" | Loop: {lm} | "
        if pl_copy:
            status_text += f"{pl_copy[cur_index]['id']} - {pl_copy[cur_index]['name']}"
        else:
            status_text += '—'
        status_var.set(status_text)
        clients_var.set(f'Clientes: {clients_count}')

    # --- Interface Tkinter ---
    root = tk.Tk()
    root.title('Rádio - Controles')
    root.geometry('540x440')
    root.configure(bg='#0b0c0f')

    style = ttk.Style()
    try:
        style.theme_use('clam')
    except Exception:
        pass
    style.configure('TFrame', background='#0b0c0f')
    style.configure('TLabel', background='#0b0c0f', foreground='#e6eef6')
    style.configure('TButton', background='#1a1c20', foreground='#9aa4b2')

    frame = ttk.Frame(root, padding=10)
    frame.pack(expand=True, fill='both')

    header = ttk.Label(frame, text='🎵 Estação Rádio — Controles', font=('Segoe UI', 11, 'bold'))
    header.pack(anchor='w', pady=(0,6))

    status_var = tk.StringVar()
    clients_var = tk.StringVar()
    loader_var = tk.StringVar()

    controls = ttk.Frame(frame)
    controls.pack(fill='x', pady=6)

    left_controls = ttk.Frame(controls)
    left_controls.pack(side='left')
    for txt, cmd in [('⏮', do_prev), ('▶', set_play), ('⏸', set_pause), ('⏭', do_next)]:
        ttk.Button(left_controls, text=txt, width=4, command=cmd).pack(side='left', padx=3)

    right_controls = ttk.Frame(controls)
    right_controls.pack(side='right')
    ttk.Label(right_controls, text='Loop:').pack(side='left', padx=(0,6))
    for txt, mode in [('None', 'off'), ('One', 'one'), ('All', 'all')]:
        ttk.Button(right_controls, text=txt, width=5, command=lambda m=mode: set_loop(m)).pack(side='left', padx=2)
    ttk.Button(right_controls, text='🔁', width=3, command=do_rescan).pack(side='left', padx=4)

    status_frame = ttk.Frame(frame)
    status_frame.pack(fill='x', pady=(6,4))
    ttk.Label(status_frame, textvariable=status_var).pack(side='left')
    loader_label = ttk.Label(status_frame, textvariable=loader_var)
    loader_label.pack(side='right')
    loader_label.pack_forget()

    pl_frame = ttk.Frame(frame)
    pl_frame.pack(fill='both', expand=True, pady=(6,4))

    lb_font = ('Segoe UI', 11, 'bold')  # fonte maior para as músicas
    lb = tk.Listbox(pl_frame, activestyle='none', bg='#081018', fg='#d7e6f6',
                    selectbackground='#173042', selectforeground='#e6eef6',
                    highlightthickness=0, borderwidth=0, font=lb_font)
    lb.pack(side='left', fill='both', expand=True)
    lb.bind('<Double-Button-1>', select_from_list)

    sc = ttk.Scrollbar(pl_frame, orient='vertical', command=lb.yview)
    sc.pack(side='right', fill='y')
    lb.config(yscrollcommand=sc.set)

    bottom = ttk.Frame(frame)
    bottom.pack(fill='x', pady=(6,0))
    ttk.Label(bottom, textvariable=clients_var).pack(side='left')
    ttk.Button(bottom, text='Ir para atual', command=lambda: lb.see(index)).pack(side='right')

    def periodic():
        try:
            update_ui()
        except Exception:
            pass
        root.after(500, periodic)

    periodic()
    root.mainloop()

def start_flask():
    ip = get_local_ip()
    log(f"Estação iniciada. Acesse http://{ip}:{PORT}/ na sua rede.")
    app.run(host="0.0.0.0", port=PORT, threaded=True)

if __name__ == "__main__":
    log(f"Pasta configurada: {MUSIC_FOLDER}")
    if not os.path.isdir(MUSIC_FOLDER):
        try:
            os.makedirs(MUSIC_FOLDER)
            log("Pasta criada pois não existia.")
        except Exception as e:
            log(f"Falha ao criar pasta: {e}")
    scan_playlist()
    with state_lock:
        paused = False
    start_broadcaster()
    threading.Thread(target=start_flask, daemon=True).start()
    start_tkinter_controls()