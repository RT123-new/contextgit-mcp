"""contextgit ui — a local point-and-click dashboard for your context store.

Zero dependencies: a stdlib ThreadingHTTPServer bound to 127.0.0.1 serving one
embedded HTML page plus a small JSON API over the same ContextGit engine the
CLI and MCP server use. Designed for people who don't want to live in a
terminal: see what the AI remembers, approve/reject pending facts, teach it
something, mark things stale, and preview exactly what a prompt's context
branch would contain.

Security model (local tool, not a web service):
* binds 127.0.0.1 only — never reachable from the network;
* every /api request must carry a per-session random token (X-ContextGit-Token)
  that is embedded in the served page, so random websites cannot drive the API
  via cross-origin fetch (they can't read the page to learn the token);
* any request with a non-localhost Origin header is rejected outright.
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from contextgit.engine import ContextGit

_ALLOWED_ORIGIN_PREFIXES = ("http://127.0.0.1", "http://localhost")


def make_server(
    engine: ContextGit,
    host: str = "127.0.0.1",
    port: int = 0,
    token: Optional[str] = None,
) -> Tuple[ThreadingHTTPServer, str]:
    """Build the dashboard server. Returns (server, session_token)."""
    session_token = token or secrets.token_hex(16)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "contextgit-ui"

        # -- helpers ----------------------------------------------------
        def _reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"error": message}, status=status)

        def _authorized(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(_ALLOWED_ORIGIN_PREFIXES):
                return False
            return self.headers.get("X-ContextGit-Token") == session_token

        def _read_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request body must be a JSON object")
            return data

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                page = _PAGE_HTML.replace("__TOKEN__", session_token)
                self._reply(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not parsed.path.startswith("/api/"):
                self._error("not found", status=404)
                return
            if not self._authorized():
                self._error("missing or bad session token", status=403)
                return
            query = parse_qs(parsed.query)
            try:
                with lock:
                    if parsed.path == "/api/status":
                        self._json(engine.status())
                    elif parsed.path == "/api/log":
                        self._json(engine.log(limit=int(query.get("n", ["25"])[0])))
                    elif parsed.path == "/api/search":
                        q = (query.get("q", [""])[0]).strip()
                        self._json(engine.search(q, limit=10) if q else [])
                    elif parsed.path == "/api/merges":
                        self._json(engine.merges(limit=15))
                    else:
                        self._error("not found", status=404)
            except Exception as exc:  # surfaced to the UI, never a stack trace
                self._error(str(exc), status=500)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized():
                self._error("missing or bad session token", status=403)
                return
            try:
                body = self._read_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(f"bad request body: {exc}")
                return
            try:
                with lock:
                    if parsed.path == "/api/branch":
                        prompt = str(body.get("prompt") or "").strip()
                        if not prompt:
                            self._error("prompt is required")
                            return
                        budget = body.get("budget")
                        self._json(engine.prepare(
                            prompt,
                            budget=int(budget) if budget else None,
                            record_usage=False,
                        ))
                    elif parsed.path == "/api/remember":
                        fact = str(body.get("fact") or "").strip()
                        if not fact:
                            self._error("fact is required")
                            return
                        self._json(engine.remember(fact, page=body.get("page") or None))
                    elif parsed.path == "/api/pending":
                        action = str(body.get("action") or "")
                        if action not in ("approve", "reject"):
                            self._error("action must be 'approve' or 'reject'")
                            return
                        self._json(engine.resolve_pending(str(body.get("content") or ""), action))
                    elif parsed.path == "/api/stale":
                        page = str(body.get("page") or "").strip()
                        if not page:
                            self._error("page is required")
                            return
                        self._json(engine.mark_stale(page))
                    elif parsed.path == "/api/demo":
                        from contextgit.demo import seed_demo
                        self._json(seed_demo(engine))
                    else:
                        self._error("not found", status=404)
            except KeyError as exc:
                self._error(str(exc), status=404)
            except Exception as exc:
                self._error(str(exc), status=500)

    server = ThreadingHTTPServer((host, port), Handler)
    return server, session_token


def run_ui(
    engine: ContextGit,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> int:
    server, token = make_server(engine, host=host, port=port)
    url = f"http://{host}:{server.server_address[1]}/?s={token[:6]}"
    print(f"contextgit ui — store: {engine.store_dir}")
    print(f"  {url}")
    print("  (local only; press Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# The page. One file, no frameworks, same visual language as the website.
# ---------------------------------------------------------------------------

_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>contextgit — your AI's memory</title>
<style>
  :root {
    --bg:#0b0e14; --bg-soft:#11151f; --panel:#151a26; --border:#232a3a;
    --text:#e6e9f0; --muted:#9aa3b5; --accent:#4ade80; --accent-dim:#1d3b2a;
    --orange:#fbbf24; --red:#f87171; --blue:#60a5fa;
    --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55}
  .wrap{max-width:980px;margin:0 auto;padding:20px 22px 60px}
  header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;padding:6px 0 18px}
  .logo{font-family:var(--mono);font-weight:700;font-size:1.15rem}
  .logo span{color:var(--accent)}
  .store{color:var(--muted);font-family:var(--mono);font-size:.78rem}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
  .stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .stat .n{font-size:1.5rem;font-weight:700}
  .stat .n.green{color:var(--accent)} .stat .n.orange{color:var(--orange)}
  .stat .l{color:var(--muted);font-size:.8rem}
  section{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px}
  section h2{font-size:1.02rem;margin-bottom:4px}
  section p.hint{color:var(--muted);font-size:.85rem;margin-bottom:12px}
  .row{display:flex;gap:10px;flex-wrap:wrap}
  input[type=text]{flex:1;min-width:220px;background:var(--bg-soft);border:1px solid var(--border);
    border-radius:8px;padding:10px 12px;color:var(--text);font-size:.95rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  button{background:var(--accent);color:#07210f;border:none;border-radius:8px;padding:10px 16px;
    font-weight:600;font-size:.9rem;cursor:pointer}
  button:hover{filter:brightness(1.12)}
  button.ghost{background:var(--bg-soft);color:var(--text);border:1px solid var(--border)}
  button.small{padding:5px 12px;font-size:.8rem;border-radius:6px}
  button.danger{background:transparent;color:var(--red);border:1px solid var(--border)}
  button.ok{background:var(--accent-dim);color:var(--accent)}
  .item{border-top:1px solid var(--border);padding:11px 2px;display:flex;gap:12px;align-items:flex-start;justify-content:space-between}
  .item:first-child{border-top:none}
  .item .body{flex:1}
  .item .ref{font-family:var(--mono);font-size:.74rem;color:var(--blue)}
  .item .txt{font-size:.92rem;margin:2px 0}
  .item .why{color:var(--muted);font-size:.8rem}
  .badge{display:inline-block;background:var(--accent-dim);color:var(--accent);border-radius:99px;
    padding:1px 10px;font-size:.75rem;font-weight:700;margin-left:8px;vertical-align:middle}
  .badge.zero{background:var(--bg-soft);color:var(--muted)}
  .preview{background:#0d1117;border:1px solid var(--border);border-radius:10px;padding:14px 16px;
    font-family:var(--mono);font-size:.8rem;white-space:pre-wrap;max-height:340px;overflow:auto;margin-top:12px}
  .meter{height:10px;background:var(--bg-soft);border-radius:99px;overflow:hidden;margin:10px 0 4px}
  .meter i{display:block;height:100%;background:var(--accent)}
  .meter-l{display:flex;justify-content:space-between;color:var(--muted);font-size:.78rem}
  .flash{position:fixed;bottom:18px;right:18px;background:var(--accent);color:#07210f;font-weight:600;
    border-radius:10px;padding:12px 18px;opacity:0;transition:opacity .25s;pointer-events:none;max-width:360px}
  .flash.err{background:var(--red);color:#2b0606}
  .flash.show{opacity:1}
  .empty{color:var(--muted);font-size:.9rem;padding:8px 0}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:760px){.cols{grid-template-columns:1fr}}
  .savebar{color:var(--muted);font-size:.85rem;margin-top:8px}
  .savebar b{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="logo">context<span>git</span> <span style="color:var(--muted);font-weight:400;font-size:.9rem">· your AI's memory</span></div>
  <div class="store" id="store"></div>
</header>

<div class="stats" id="stats"></div>

<section>
  <h2>What would my AI see?</h2>
  <p class="hint">Type any question. This shows the exact compact context contextgit would hand your AI — and what it saves you vs. sending the whole history.</p>
  <div class="row">
    <input type="text" id="ask" placeholder='e.g. "What database does Atlas use?"'>
    <button onclick="preview()">Preview context</button>
  </div>
  <div id="previewBox" style="display:none">
    <div class="meter"><i id="meterFill" style="width:0%"></i></div>
    <div class="meter-l"><span id="meterLeft"></span><span id="meterRight"></span></div>
    <div class="savebar" id="saveLine"></div>
    <div class="preview" id="previewText"></div>
  </div>
</section>

<section>
  <h2>Waiting for your approval<span class="badge zero" id="pendingBadge">0</span></h2>
  <p class="hint">Facts your AI wasn't sure it should keep. Approve to remember them forever, reject to drop them.</p>
  <div id="pending"><div class="empty">Nothing waiting — you're all caught up.</div></div>
</section>

<div class="cols">
  <section>
    <h2>Teach it something</h2>
    <p class="hint">Saved instantly as a durable fact, with full provenance.</p>
    <div class="row">
      <input type="text" id="fact" placeholder='e.g. "We deploy on Fridays at 10am"'>
      <button onclick="remember()">Save</button>
    </div>
  </section>
  <section>
    <h2>Find a memory</h2>
    <p class="hint">Search everything it has ever recorded.</p>
    <div class="row">
      <input type="text" id="q" placeholder="search…" oninput="searchSoon()">
    </div>
    <div id="results"></div>
  </section>
</div>

<section>
  <h2>Recent activity</h2>
  <p class="hint">The newest entries in the journal — every conversation turn and saved fact lands here.</p>
  <div id="log"><div class="empty">No activity yet.</div></div>
  <div class="row" style="margin-top:12px" id="demoRow" hidden>
    <button class="ghost" onclick="demo()">Load sample data to explore</button>
  </div>
</section>

<div class="flash" id="flash"></div>
</div>

<script>
const TOKEN = "__TOKEN__";
const H = {"Content-Type":"application/json","X-ContextGit-Token":TOKEN};
const $ = id => document.getElementById(id);
// Button payloads live in these arrays and are referenced by index via
// data attributes — content with quotes must never be interpolated into
// inline handlers.
let PENDING = [];
let RESULTS = [];

function flash(msg, err=false){
  const f=$("flash"); f.textContent=msg; f.className="flash show"+(err?" err":"");
  setTimeout(()=>f.className="flash"+(err?" err":""), 2600);
}
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:H}, opts||{}));
  const data = await r.json();
  if(!r.ok) throw new Error(data.error||("HTTP "+r.status));
  return data;
}
function esc(s){const d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML}

async function refresh(){
  try{
    const s = await api("/api/status");
    $("store").textContent = s.store_dir;
    $("stats").innerHTML = `
      <div class="stat"><div class="n">${s.events}</div><div class="l">things recorded</div></div>
      <div class="stat"><div class="n">${s.wiki_pages_active}</div><div class="l">facts it knows now</div></div>
      <div class="stat"><div class="n orange">${s.pending_merges}</div><div class="l">waiting for approval</div></div>
      <div class="stat"><div class="n green">${(s.usage.saved_tokens_total).toLocaleString()}</div><div class="l">tokens saved (${s.usage.savings_pct}%)</div></div>`;
    $("demoRow").hidden = s.events > 0;

    const m = await api("/api/merges");
    const badge = $("pendingBadge");
    badge.textContent = m.pending.length;
    badge.className = "badge" + (m.pending.length ? "" : " zero");
    PENDING = m.pending;
    $("pending").innerHTML = m.pending.length ? m.pending.map((p,i)=>`
      <div class="item"><div class="body">
        <div class="txt">${esc(p.content)}</div>
        <div class="why">why it waited: ${esc(p.reason||"uncertain")}</div></div>
        <div class="row" style="flex-wrap:nowrap">
          <button class="small ok" data-i="${i}" data-act="approve">Approve</button>
          <button class="small danger" data-i="${i}" data-act="reject">Reject</button>
        </div></div>`).join("")
      : `<div class="empty">Nothing waiting — you're all caught up.</div>`;

    const log = await api("/api/log?n=12");
    $("log").innerHTML = log.length ? log.map(r=>`
      <div class="item"><div class="body">
        <span class="ref">${esc(r.ref)}</span>
        <div class="txt">${esc(r.summary)}</div>
        <div class="why">${esc(r.timestamp)} · ${esc(r.speaker)} · ${r.tokens} tokens</div>
      </div></div>`).join("") : `<div class="empty">No activity yet.</div>`;
  }catch(e){ flash(e.message, true); }
}

async function preview(){
  const prompt = $("ask").value.trim();
  if(!prompt) return flash("Type a question first", true);
  try{
    const r = await api("/api/branch",{method:"POST",body:JSON.stringify({prompt})});
    $("previewBox").style.display="block";
    const pct = Math.max(2, Math.round(100*r.estimated_tokens/Math.max(1,r.full_history_tokens)));
    $("meterFill").style.width = pct+"%";
    $("meterLeft").textContent = `sends ${r.estimated_tokens} tokens`;
    $("meterRight").textContent = `full history: ${r.full_history_tokens} tokens`;
    $("saveLine").innerHTML = `That's <b>${r.savings_pct}% saved</b> on this question (${r.saved_tokens.toLocaleString()} tokens), built from ${r.selected.length} of ${r.source_count} memories.`;
    $("previewText").textContent = r.context;
  }catch(e){ flash(e.message, true); }
}

async function remember(){
  const fact = $("fact").value.trim();
  if(!fact) return flash("Type the fact first", true);
  try{
    const r = await api("/api/remember",{method:"POST",body:JSON.stringify({fact})});
    $("fact").value="";
    flash(`Saved to "${r.target_page}"`);
    refresh();
  }catch(e){ flash(e.message, true); }
}

async function resolvePending(content, action){
  try{
    await api("/api/pending",{method:"POST",body:JSON.stringify({content, action})});
    flash(action==="approve" ? "Approved — it will remember this." : "Rejected — dropped.");
    refresh();
  }catch(e){ flash(e.message, true); }
}

let _t=null;
function searchSoon(){ clearTimeout(_t); _t=setTimeout(search, 250); }
async function search(){
  const q=$("q").value.trim();
  if(!q){ $("results").innerHTML=""; return; }
  try{
    const rows = await api("/api/search?q="+encodeURIComponent(q));
    RESULTS = rows;
    $("results").innerHTML = rows.length ? rows.map((r,i)=>`
      <div class="item"><div class="body">
        <span class="ref">${esc(r.ref)}</span>
        <div class="txt">${esc(r.summary)}</div></div>
        ${r.ref.startsWith("wiki:") ? `<button class="small danger" data-i="${i}" data-act="stale">Mark outdated</button>`:""}
      </div>`).join("") : `<div class="empty">No matches.</div>`;
  }catch(e){ flash(e.message, true); }
}

async function stale(page){
  try{
    await api("/api/stale",{method:"POST",body:JSON.stringify({page})});
    flash(`Marked "${page}" as outdated — it won't be used again.`);
    search(); refresh();
  }catch(e){ flash(e.message, true); }
}

async function demo(){
  try{
    await api("/api/demo",{method:"POST",body:"{}"});
    flash("Sample data loaded — try the preview above!");
    $("ask").value = "What database does Atlas use?";
    refresh();
  }catch(e){ flash(e.message, true); }
}

$("pending").addEventListener("click", e => {
  const b = e.target.closest("button[data-act]");
  if (b) resolvePending(PENDING[+b.dataset.i].content, b.dataset.act);
});
$("results").addEventListener("click", e => {
  const b = e.target.closest("button[data-act='stale']");
  if (b) stale(RESULTS[+b.dataset.i].ref.slice(5));
});
$("ask").addEventListener("keydown",e=>{if(e.key==="Enter")preview()});
$("fact").addEventListener("keydown",e=>{if(e.key==="Enter")remember()});
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""
