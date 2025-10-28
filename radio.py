#!/usr/bin/env python3
# radio_fixed.py
# Versão com logs, filtro de extensões, botão de rescan e rota de debug.

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
playlist = []
index = 0
paused = True
loop_mode = "none"
broadcaster_thread = None
broadcaster_stop = threading.Event()
clients = set()
skip_event = threading.Event()
action_pending = None

def log(msg):
    print(f"[radio] {msg}", flush=True)

def scan_playlist():
    """Atualiza a lista de músicas; trata erros e filtra por extensão."""
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
        playlist = found
        log(f"scan_playlist: encontrou {len(playlist)} arquivo(s).")
        for i,p in enumerate(playlist):
            log(f"  {i}: {p}")
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
    """
    Thread que só inicia o ffmpeg quando houver uma faixa e o estado não estiver pausado.
    Evita processos ffmpeg simultâneos, espera o processo anterior terminar ao fazer skip
    e limpa os buffers das filas dos clientes para evitar sobreposição (duplo áudio).
    """
    global index, paused, loop_mode, action_pending
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
            # menor latência na checagem do estado pausado
            time.sleep(0.05)
            if broadcaster_stop.is_set():
                return

        with state_lock:
            current = playlist[index]

        if not os.path.isfile(current):
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

        cmd = [FFMPEG_BIN, "-re", "-i", current, "-vn", "-f", "mp3", "-ab", "192k", "pipe:1", "-loglevel", "error"]
        log(f"Tocando: {current}")
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

                # skip/prev via Tkinter ou endpoint
                if skip_event.is_set():
                    with state_lock:
                        if action_pending == "next":
                            index = (index + 1) % len(playlist)
                        elif action_pending == "prev":
                            index = (index - 1 + len(playlist)) % len(playlist)
                        action_pending = None
                        manual_advance = True  # marca que já avançou manualmente
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
                    # checagem mais frequente para reduzir latência entre clique e ação
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
            "playlist": [os.path.basename(p) for p in playlist],
            "index": index,
            "current": os.path.basename(cur) if cur else None,
            "paused": paused,
            "loop": loop_mode,
            "clients": len(clients)
        }
    return jsonify(info)

# Frontend HTML com JS ajustado para otimistic UI (apenas frontend foi alterado para melhorar a experiência)
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Estação Rádio - Player</title>
  <style>
    :root{
      --bg:#f6f8fb;
      --card:#ffffff;
      --muted:#6b7280;
      --accent:#0ea5b7;
      --accent-2:#6d28d9;
    }
    html,body{height:100%;margin:0;font-family:Inter,ui-sans-serif,system-ui,Segoe UI,Roboto,"Helvetica Neue",Arial; background:linear-gradient(180deg,#f7fbff 0%, #eef5fb 100%);color:#000}
    .wrap{max-width:900px;margin:28px auto;padding:20px}
    .card{background:var(--card);border-radius:12px;padding:18px;box-shadow:0 8px 30px rgba(2,6,23,0.06)}
    h1{margin:0 0 8px;font-size:20px}
    .controls{display:flex;gap:8px;align-items:center;margin:12px 0}
    button.btn{background:transparent;border:1px solid rgba(15,23,42,0.06);padding:10px 14px;border-radius:8px;color:var(--accent);cursor:pointer;font-weight:600;display:inline-flex;align-items:center;gap:8px}
    button.btn:disabled{opacity:0.5;cursor:default}
    .btn.secondary{color:var(--muted);border-style:dashed}
    .status{color:var(--muted);font-size:14px;margin-top:6px}
    .right{margin-left:auto}
    .playlist-row{display:flex;gap:12px;align-items:center;margin-top:12px}
    select#trackSelect{flex:1;padding:10px;border-radius:8px;border:1px solid rgba(0,0,0,0.06);background:transparent;color:inherit}
    ul#pl{list-style:none;padding:0;margin:12px 0 0;max-height:220px;overflow:auto}
    li{padding:6px 8px;border-radius:6px;color:#0b1220}
    li.active{background:linear-gradient(90deg, rgba(14,165,183,0.08), rgba(109,40,217,0.04));border:1px solid rgba(0,0,0,0.03)}
    .meta{font-size:13px;color:var(--muted)}
    .loader{width:16px;height:16px;border-radius:50%;border:2px solid rgba(0,0,0,0.08);border-top-color:var(--accent);animation:spin .9s linear infinite;display:inline-block;margin-left:6px;vertical-align:middle}
    .hidden{display:none}
    @keyframes spin{to{transform:rotate(360deg)}}
    .small{font-size:13px;padding:6px 8px}
    audio{width:100%;margin-top:12px;border-radius:8px;background:transparent}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Estação Rádio</h1>
      <div class="meta">Servidor de streaming local — acesse <code>/stream</code> para receber áudio (alternativa sem controles).</div>

      <!-- Player embutido na página: autoplay ao abrir -->
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

      <div class="playlist-row">
        <label for="trackSelect" class="meta">Escolha uma música:</label>
        <select id="trackSelect"></select>
        <button id="btnSelectPlay" class="btn small">Tocar selecionada <span id="loaderSelect" class="loader hidden"></span></button>
      </div>

      <div id="statusText" class="status">Carregando estado...</div>

      <div id="playlist" style="display:none;">
        <h3 style="margin-top:18px">Playlist</h3>
        <ul id="pl"></ul>
      </div>
    </div>
  </div>

<script>
  let lastStatus = { playlist: [], index: 0, paused: true, loop: 'none' };
  let player = null;
  let suppressRefreshUntil = 0; // quando setado, o refresh não sobrescreve o índice por um tempo
  let selectionInProgress = false;

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

  // UI actions with loaders
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

  // Optimistic UI and background calls
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
      li.textContent = p;
      if (i===lastStatus.index) li.classList.add('active');
      pl.appendChild(li);
    });

    // fill select options
    const sel = document.getElementById('trackSelect');
    const prevSel = sel.value;
    sel.innerHTML = '';
    lastStatus.playlist.forEach((p,i)=>{
      const opt = document.createElement('option');
      opt.value = i; opt.text = p;
      sel.appendChild(opt);
    });
    // só atualiza a seleção se não estivermos em período de supressão
    if (lastStatus.playlist.length>0 && Date.now() >= suppressRefreshUntil){
      sel.selectedIndex = lastStatus.index;
    }

    const statusEl = document.getElementById('statusText');
    statusEl.innerText = (lastStatus.paused ? '⏸️ Pausado' : '▶️ Tocando') + ' | Loop: ' + (lastStatus.loop || 'none') + ' | Faixa: ' + (lastStatus.playlist[lastStatus.index] || '—');

    // toggle playlist visibility based on size
    document.getElementById('playlist').style.display = lastStatus.playlist.length>0 ? 'block' : 'none';
  }

  async function refreshStatus(){
    try{
      const r = await fetch('/status'); if(!r.ok) return;
      const j = await r.json();
      const now = Date.now();
      lastStatus.playlist = j.playlist || [];
      // se estivermos suprimindo updates de índice, não sobrescrever
      if (now >= suppressRefreshUntil){
        lastStatus.index = (typeof j.index === 'number') ? j.index : 0;
      }
      lastStatus.paused = !!j.paused;
      lastStatus.loop = j.loop || j.loop_mode || 'none';
      renderFromLastStatus();
    }catch(e){ console.error('refreshStatus', e); }
  }

  // When user selects a track and hits "Tocar selecionada":
  async function playSelected(){
    const sel = document.getElementById('trackSelect');
    if(!sel || !sel.options.length) return;
    const desired = parseInt(sel.value);
    const n = lastStatus.playlist.length; if(n===0) return;
    const current = lastStatus.index;
    if(desired===current){
      // apenas garante que está tocando
      await doControlWithLoader('/play','loaderSelect');
      try{ player.play().catch(()=>{}); }catch(e){}
      return;
    }

    // decide direção com menor passos
    const forward = (desired - current + n) % n;
    const backward = (current - desired + n) % n;
    const action = forward <= backward ? '/next' : '/prev';
    const steps = Math.min(forward, backward);

    showLoader('loaderSelect', true);
    // otimistic set
    lastStatus.index = desired; renderFromLastStatus();
    // suprimir atualizações que sobrescrevam o índice por alguns instantes
    suppressRefreshUntil = Date.now() + 2500;

    for(let i=0;i<steps;i++){
      try{ await callEndpoint(action); }catch(e){}
      // pequeno intervalo para dar tempo ao servidor processar
      await new Promise(r=>setTimeout(r, 120));
    }

    // request play to ensure playback
    try{ await callEndpoint('/play'); }catch(e){}

    // wait until server reports the expected index or timeout
    const timeoutAt = Date.now()+3500;
    while(Date.now()<timeoutAt){
      await refreshStatus();
      if(lastStatus.index===desired) break;
      await new Promise(r=>setTimeout(r, 150));
    }

    showLoader('loaderSelect', false);
    renderFromLastStatus();
  }

  document.addEventListener('DOMContentLoaded', ()=>{
    player = document.getElementById('player');
    document.getElementById('btnPlay').addEventListener('click', ()=>doControl('/play'));
    document.getElementById('btnPause').addEventListener('click', ()=>doControl('/pause'));
    document.getElementById('btnNext').addEventListener('click', ()=>doControl('/next'));
    document.getElementById('btnPrev').addEventListener('click', ()=>doControl('/prev'));
    document.getElementById('btnRescan').addEventListener('click', ()=>rescan());
    document.getElementById('btnSelectPlay').addEventListener('click', ()=>playSelected());
    document.getElementById('loop').addEventListener('change', ()=>setLoop());
    // Quando o usuário muda manualmente a dropdown, tocar automaticamente e suprimir refresh
    const trackSel = document.getElementById('trackSelect');
    trackSel.addEventListener('change', ()=>{
      suppressRefreshUntil = Date.now() + 2500;
      playSelected().catch(()=>{});
    });

    refreshStatus();
    setInterval(refreshStatus, 900);
    // Ao abrir a página, tentar reproduzir imediatamente (se estiver pausado)
    (async ()=>{
      try{
        await refreshStatus();
        if(lastStatus.paused){
          // chama o controle de play de forma não bloqueante
          doControl('/play').catch(()=>{});
        }
        // tenta também tocar o elemento de áudio local (pode ser bloqueado pelo navegador)
        try{ player.play().catch(()=>{}); }catch(e){}
      }catch(e){/* silencioso */}
    })();
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
    return jsonify([os.path.basename(p) for p in playlist])

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
    global paused, index, loop_mode
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

def start_tkinter_controls():
    """Cria uma janela Tkinter com controles simples para o rádio."""
    def do_play(): send_backend_action("play")
    def do_pause(): send_backend_action("pause")
    def do_next(): send_backend_action("next")
    def do_prev(): send_backend_action("prev")
    def set_loop_mode(mode):
        send_backend_action(f"loop_{mode}")

    def send_backend_action(action):
        """Aciona a função /control do backend diretamente."""
        global paused, index, loop_mode
        with state_lock:
            if action == "play":
                paused = False
            elif action == "pause":
                paused = True
            elif action in ("next", "prev"):
                global action_pending
                action_pending = action  # action será "next" ou "prev"
                skip_event.set()  # apenas sinaliza que queremos pular/faixa anterior
            elif action == "loop_one":
                loop_mode = "one"
            elif action == "loop_all":
                loop_mode = "all"
            elif action == "loop_off":
                loop_mode = "none"
        current = playlist[index] if playlist else None
        log(f"[Tkinter] Ação: {action}, faixa atual: {current}")
        update_status_label()

    def update_status_label():
        """Atualiza o status a partir do índice atual de forma segura."""
        with state_lock:
            if not playlist:
                status_var.set("Playlist vazia")
            else:
                status_var.set(f"{'⏸️ Pausado' if paused else '▶️ Tocando'} | "
                               f"{os.path.basename(playlist[index])} | Loop: {loop_mode}")

    # Função que atualiza periodicamente
    def periodic_update():
        update_status_label()
        root.after(150, periodic_update)  # atualiza a cada 0.3s

    root = tk.Tk()
    root.title("Rádio Backend Controls")
    root.geometry("400x150")

    frame = ttk.Frame(root, padding=10)
    frame.pack(expand=True, fill="both")

    status_var = tk.StringVar()
    status_label = ttk.Label(frame, textvariable=status_var)
    status_label.pack(pady=5)

    buttons_frame = ttk.Frame(frame)
    buttons_frame.pack(pady=5)

    ttk.Button(buttons_frame, text="⏮ Prev", command=do_prev).grid(row=0, column=0, padx=5)
    ttk.Button(buttons_frame, text="▶ Play", command=do_play).grid(row=0, column=1, padx=5)
    ttk.Button(buttons_frame, text="⏸ Pause", command=do_pause).grid(row=0, column=2, padx=5)
    ttk.Button(buttons_frame, text="⏭ Next", command=do_next).grid(row=0, column=3, padx=5)

    loop_frame = ttk.Frame(frame)
    loop_frame.pack(pady=5)
    ttk.Label(loop_frame, text="Loop:").pack(side="left", padx=5)
    ttk.Button(loop_frame, text="None", command=lambda: set_loop_mode("off")).pack(side="left", padx=2)
    ttk.Button(loop_frame, text="One", command=lambda: set_loop_mode("one")).pack(side="left", padx=2)
    ttk.Button(loop_frame, text="All", command=lambda: set_loop_mode("all")).pack(side="left", padx=2)

    update_status_label()
    periodic_update()
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
    #ip = get_local_ip()
    #log(f"Estação iniciada. Acesse http://{ip}:{PORT}/ na sua rede.")
    #app.run(host="0.0.0.0", port=PORT, threaded=True)