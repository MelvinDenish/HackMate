"""
╔══════════════════════════════════════════════════════════════╗
║  PROGRESS SERVER — Real-Time WebSocket Progress Dashboard    ║
║                                                              ║
║  Inspired by: Bolt.new (live build view), Devin (web UI)     ║
║                                                              ║
║  Serves a real-time dashboard showing pipeline progress.     ║
║  Uses WebSockets to broadcast phase updates, cost, and       ║
║  file creation events to any connected browser.              ║
║                                                              ║
║  Start: python -m pipeline.progress_server                   ║
║  View:  http://localhost:8765                                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Event queue for broadcasting to WebSocket clients
_event_queue: list[dict] = []
_ws_clients: list = []

# Server state
_server_running = False
_start_time: Optional[float] = None


def record_event(
    phase: str,
    status: str,
    message: str = "",
    cost_usd: float = 0.0,
    files: list[str] = None,
) -> None:
    """Record a pipeline event for broadcasting.

    Can be called from any agent/node — events accumulate and
    are broadcast to connected WebSocket clients.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed": time.time() - _start_time if _start_time else 0,
        "phase": phase,
        "status": status,
        "message": message,
        "cost_usd": cost_usd,
        "files": files or [],
    }
    _event_queue.append(event)

    # Broadcast to connected clients
    for ws_send in list(_ws_clients):
        try:
            asyncio.get_event_loop().create_task(
                ws_send(json.dumps(event))
            )
        except Exception:
            pass


def start_tracking() -> None:
    """Mark the start of a pipeline run."""
    global _start_time, _event_queue
    _start_time = time.time()
    _event_queue = []
    record_event("pipeline", "started", "Pipeline execution beginning")


def get_all_events() -> list[dict]:
    """Get all recorded events (for initial page load)."""
    return list(_event_queue)


def create_progress_callback():
    """Create a progress_callback compatible with build_pipeline()."""
    def callback(phase: str, status: str, cost_so_far: float = 0.0):
        record_event(phase, status, cost_usd=cost_so_far)
    return callback


# ── Dashboard HTML ───────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HackMate — Pipeline Dashboard</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: hsl(222, 47%, 5%);
      color: hsl(210, 40%, 96%);
      padding: 24px;
      min-height: 100vh;
    }
    h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 8px; }
    .subtitle { color: hsl(215, 20%, 55%); font-size: 0.875rem; margin-bottom: 24px; }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: hsl(222, 47%, 11%);
      border: 1px solid hsl(222, 20%, 18%);
      border-radius: 10px;
      padding: 16px;
    }
    .stat-label { font-size: 0.75rem; color: hsl(215, 20%, 55%); text-transform: uppercase; letter-spacing: 0.05em; }
    .stat-value { font-size: 1.5rem; font-weight: 600; margin-top: 4px; }
    .stat-value.cost { color: hsl(142, 71%, 45%); }
    .stat-value.time { color: hsl(199, 89%, 48%); }
    .phases {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 24px;
    }
    .phase {
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 0.8rem;
      font-weight: 500;
      border: 1px solid hsl(222, 20%, 18%);
      background: hsl(222, 47%, 11%);
      transition: all 200ms ease;
    }
    .phase.pending { opacity: 0.4; }
    .phase.running {
      background: hsl(262, 83%, 20%);
      border-color: hsl(262, 83%, 40%);
      color: hsl(262, 83%, 80%);
      animation: pulse 1.5s infinite;
    }
    .phase.completed {
      background: hsl(142, 60%, 15%);
      border-color: hsl(142, 60%, 30%);
      color: hsl(142, 71%, 65%);
    }
    .phase.failed {
      background: hsl(0, 60%, 15%);
      border-color: hsl(0, 60%, 30%);
      color: hsl(0, 84%, 70%);
    }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
    .events {
      background: hsl(222, 47%, 8%);
      border: 1px solid hsl(222, 20%, 18%);
      border-radius: 10px;
      padding: 16px;
      max-height: 500px;
      overflow-y: auto;
    }
    .event {
      padding: 8px 0;
      border-bottom: 1px solid hsl(222, 20%, 12%);
      font-size: 0.85rem;
      display: flex;
      gap: 12px;
    }
    .event:last-child { border-bottom: none; }
    .event-time { color: hsl(215, 20%, 45%); font-family: monospace; font-size: 0.75rem; min-width: 60px; }
    .event-phase { font-weight: 500; color: hsl(262, 83%, 70%); min-width: 100px; }
    .event-msg { color: hsl(215, 20%, 75%); }
    .connected { color: hsl(142, 71%, 55%); }
    .disconnected { color: hsl(0, 84%, 60%); }
  </style>
</head>
<body>
  <h1>🚀 HackMate Pipeline Dashboard</h1>
  <p class="subtitle">Real-time pipeline execution monitor • <span id="ws-status" class="disconnected">Connecting...</span></p>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-label">Elapsed Time</div>
      <div class="stat-value time" id="elapsed">0:00</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Cost</div>
      <div class="stat-value cost" id="cost">$0.0000</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Files Generated</div>
      <div class="stat-value" id="files">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Current Phase</div>
      <div class="stat-value" id="current-phase">—</div>
    </div>
  </div>

  <div class="phases" id="phase-badges"></div>

  <h3 style="margin-bottom: 12px; font-size: 1rem;">Event Log</h3>
  <div class="events" id="events"></div>

  <script>
    const phases = ['clarification','research','architecture','planning','coding',
                    'deslopify','review','security','deployment','pitch','presentation'];
    const phaseStatus = {};
    phases.forEach(p => phaseStatus[p] = 'pending');

    let totalCost = 0;
    let fileCount = 0;
    let startTime = Date.now();

    function renderPhases() {
      const container = document.getElementById('phase-badges');
      container.innerHTML = phases.map(p =>
        `<span class="phase ${phaseStatus[p]}">${p}</span>`
      ).join('');
    }

    function addEvent(evt) {
      const events = document.getElementById('events');
      const elapsed = evt.elapsed ? Math.round(evt.elapsed) : 0;
      const mins = Math.floor(elapsed / 60);
      const secs = elapsed % 60;
      const div = document.createElement('div');
      div.className = 'event';
      div.innerHTML = `
        <span class="event-time">${mins}:${String(secs).padStart(2, '0')}</span>
        <span class="event-phase">${evt.phase}</span>
        <span class="event-msg">${evt.status}${evt.message ? ' — ' + evt.message : ''}</span>
      `;
      events.prepend(div);
    }

    function updateStats() {
      const elapsed = Math.round((Date.now() - startTime) / 1000);
      const mins = Math.floor(elapsed / 60);
      const secs = elapsed % 60;
      document.getElementById('elapsed').textContent = `${mins}:${String(secs).padStart(2, '0')}`;
      document.getElementById('cost').textContent = `$${totalCost.toFixed(4)}`;
      document.getElementById('files').textContent = fileCount;
    }

    renderPhases();
    setInterval(updateStats, 1000);

    // WebSocket connection
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
      document.getElementById('ws-status').textContent = 'Connected';
      document.getElementById('ws-status').className = 'connected';
    };
    ws.onclose = () => {
      document.getElementById('ws-status').textContent = 'Disconnected';
      document.getElementById('ws-status').className = 'disconnected';
    };
    ws.onmessage = (e) => {
      const evt = JSON.parse(e.data);
      if (evt.phase && evt.status) {
        phaseStatus[evt.phase] = evt.status;
        document.getElementById('current-phase').textContent = evt.phase;
        renderPhases();
      }
      if (evt.cost_usd) totalCost += evt.cost_usd;
      if (evt.files) fileCount += evt.files.length;
      addEvent(evt);
      updateStats();
    };
  </script>
</body>
</html>"""


async def _start_server(host: str = "0.0.0.0", port: int = 8765):
    """Start the WebSocket progress server."""
    global _server_running

    try:
        from aiohttp import web
    except ImportError:
        logger.warning("[ProgressServer] aiohttp not installed — using fallback")
        return

    app = web.Application()

    async def handle_index(request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def handle_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        _ws_clients.append(ws.send_str)

        # Send all existing events
        for event in _event_queue:
            await ws.send_str(json.dumps(event))

        try:
            async for msg in ws:
                pass  # We only send, never receive
        finally:
            _ws_clients.remove(ws.send_str)

        return ws

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _server_running = True
    logger.info(f"[ProgressServer] Dashboard at http://localhost:{port}")

    # Keep running
    while _server_running:
        await asyncio.sleep(1)

    await runner.cleanup()


def start_server_background(port: int = 8765) -> None:
    """Start the progress server in a background thread."""
    import threading

    def _run():
        asyncio.run(_start_server(port=port))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"[ProgressServer] Background thread started on port {port}")


def stop_server() -> None:
    """Signal the server to stop."""
    global _server_running
    _server_running = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Starting HackMate Progress Dashboard...")
    print("Open: http://localhost:8765")
    asyncio.run(_start_server())
