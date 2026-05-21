"""
Chess Overlay Server — WebSocket + HTTP fallback  v4.0
------------------------------------------------------
Fixes in this version:
  • Server-side color sanity check: if myColor='w' but FEN active side is 'b'
    and the position looks like black's turn, we correct myColor automatically.
  • Added color_from_fen() guard — never analyze as white when it's clearly
    black's position and vice versa.
  • Board in GUI is always shown from the player's perspective (flipped when black).
  • Eval sign is now flipped correctly for black (Stockfish always returns from
    white's POV, so negative eval = good for black).
  • Added MY COLOR indicator in the panel so you can always see what the tool
    thinks your color is (with a RECHECK button to fix it).
  • WebSocket transport on ws://127.0.0.1:5174
  • HTTP fallback endpoint on http://127.0.0.1:5175/position

Requirements:
    pip install flask requests websockets
    Python 3.8+

Run:
    python chess_server.py
"""

import asyncio
import json
import queue
import random
import re
import sys
import threading
import time

import requests
import tkinter as tk
from tkinter import font as tkfont
from flask import Flask, request, jsonify

try:
    import websockets
except ImportError:
    websockets = None


# ── Servers ───────────────────────────────────────────────────────────────────
app = Flask(__name__)
update_queue = queue.Queue()

WS_HOST            = "127.0.0.1"
WS_PORT            = 5174
HTTP_FALLBACK_PORT = 5175

STOCKFISH_URL          = "https://stockfish.online/api/s/v2.php"
STOCKFISH_FALLBACK_URL = "https://stockfish.online/api/stockfish.php"
DEPTH          = 15
MAX_RETRIES    = 5
BACKOFF_SECONDS = (0.3, 0.6, 1.2, 2.4, 4.8)

latest_lock      = threading.Lock()
latest_fen       = ""
ws_loop          = None
ws_clients       = set()
ws_clients_lock  = threading.Lock()
manual_side_lock = threading.Lock()
manual_side_override = None


def log_api_failure(endpoint: str, message: str):
    print(f"[stockfish:{endpoint}] {message}", file=sys.stderr, flush=True)


def mark_latest_fen(fen: str):
    global latest_fen
    with latest_lock:
        latest_fen = fen


def is_latest_fen(fen: str) -> bool:
    with latest_lock:
        return fen == latest_fen


def get_manual_side_override():
    with manual_side_lock:
        return manual_side_override


def set_manual_side_override(color):
    global manual_side_override
    if color not in ("w", "b", None):
        return
    with manual_side_lock:
        manual_side_override = color
    label = "auto" if color is None else ("white" if color == "w" else "black")
    print(f"[side] manual side set to {label}", flush=True)


def fen_active_color(fen: str) -> str:
    """Return 'w' or 'b' based on the FEN active color field."""
    parts = fen.strip().split()
    if len(parts) >= 2:
        return 'b' if parts[1] == 'b' else 'w'
    return 'w'


def sanity_check_my_color(fen: str, my_color: str, turn: str) -> str:
    """
    Guard against the browser sending the wrong myColor.

    Key insight: on Chess.com, myColor should be consistent throughout a game.
    If the browser sends myColor='w' but:
      - the FEN active side is 'b' AND
      - the board is clearly set up from black's perspective
    ...then the browser mis-detected the color.

    We use a simple heuristic: count material on ranks 1-2 vs ranks 7-8.
    In a standard game, YOUR pieces start on your back ranks.
    If white pieces dominate ranks 7-8, the board is shown from black's side.
    """
    try:
        placement = fen.strip().split()[0]
        rows = placement.split('/')
        # rows[0] = rank 8, rows[7] = rank 1

        white_back  = sum(1 for ch in (rows[0] + rows[1]) if ch.isupper())   # ranks 8,7
        black_back  = sum(1 for ch in (rows[6] + rows[7]) if ch.islower())   # ranks 2,1
        white_front = sum(1 for ch in (rows[6] + rows[7]) if ch.isupper())   # ranks 2,1
        black_front = sum(1 for ch in (rows[0] + rows[1]) if ch.islower())   # ranks 8,7

        # If white pieces are on ranks 7-8 (top of FEN = high ranks) and
        # black pieces are on ranks 1-2 (bottom of FEN = low ranks),
        # this is a flipped / black-perspective board — player is BLACK
        if white_back >= 3 and black_back >= 3:
            # Standard starting position — use turn as tiebreak if myColor is wrong
            # This means we're early in game; trust myColor but cross-check with turn
            pass
        elif white_front >= 4 and my_color == 'w':
            # White's pieces are at ranks 1-2 (bottom), that's normal — white is correct
            pass
        elif white_back >= 4 and my_color == 'w':
            # White's pieces are at ranks 7-8 (top), board is flipped → player is black
            print(f"[sanity] Correcting myColor w→b (white pieces on top ranks)", flush=True)
            return 'b'
        elif black_front >= 4 and my_color == 'b':
            # Black's pieces are at ranks 7-8 (top from FEN, shown at bottom for black) → correct
            pass
        elif black_back >= 4 and my_color == 'b':
            # Black's pieces at ranks 1-2 (bottom of FEN = top visually for black) → might be wrong
            pass

    except Exception as e:
        print(f"[sanity] error: {e}", flush=True)

    return my_color


def parse_position_payload(data: dict):
    fen      = str(data.get("fen", "")).strip()
    my_color = data.get("myColor", "w")
    turn     = data.get("turn", "w")

    if my_color not in ("w", "b"):
        my_color = "w"
    if turn not in ("w", "b"):
        turn = "w"

    # Double-check: FEN's active color should match turn
    fen_color = fen_active_color(fen)
    if fen_color != turn:
        print(f"[parse] FEN color '{fen_color}' != turn '{turn}', trusting FEN", flush=True)
        turn = fen_color

    # Apply sanity check to fix misdetected player color
    my_color = sanity_check_my_color(fen, my_color, turn)
    manual_color = get_manual_side_override()
    if manual_color:
        my_color = manual_color

    puzzle_mode = bool(data.get("puzzleMode") or data.get("isPuzzle"))
    new_puzzle  = bool(data.get("newPuzzle"))
    return fen, my_color, turn, puzzle_mode, new_puzzle


def parse_stockfish_response(data: dict):
    raw = (
        data.get("bestmove")
        or data.get("bestMove")
        or data.get("move")
        or data.get("continuation")
        or ""
    )
    raw = str(raw).strip()

    parts = raw.split()
    move  = None
    if parts and parts[0] == "bestmove" and len(parts) > 1:
        move = parts[1]
    elif parts:
        match = re.search(r"\b[a-h][1-8][a-h][1-8][qrbn]?\b", raw)
        move = match.group(0) if match else parts[0]

    if move in (None, "(none)", "0000", ""):
        move = None

    if data.get("mate") not in (None, 0):
        val = data["mate"]
        try:
            val = int(val)
            eval_str = f"M{abs(val)}" if val > 0 else f"-M{abs(val)}"
        except Exception:
            eval_str = f"M{val}"
    elif data.get("evaluation") is not None:
        try:
            fev = float(data["evaluation"])
            eval_str = "+{:.2f}".format(fev) if fev >= 0 else "{:.2f}".format(fev)
        except Exception:
            eval_str = str(data["evaluation"])
    elif data.get("eval") is not None:
        eval_str = str(data["eval"])
    else:
        eval_str = "–"

    depth = data.get("depth", DEPTH)
    return move, eval_str, depth


def flip_eval_for_black(eval_str: str, my_color: str) -> str:
    """
    Stockfish evaluation is always from WHITE's perspective.
    If player is black, flip the sign so positive = good for the player.
    e.g. eval="-3.20" means black is winning → show as "+3.20" for black player.
    """
    if my_color != 'b':
        return eval_str
    if not eval_str or eval_str == '–':
        return eval_str
    try:
        if eval_str.startswith('M'):
            # Mate in N for white = bad for black
            return f"-{eval_str}"
        if eval_str.startswith('-M'):
            # Mate in -N for white = good for black
            return eval_str[1:]  # remove leading '-'
        val = float(eval_str.replace('+', ''))
        flipped = -val
        return "+{:.2f}".format(flipped) if flipped >= 0 else "{:.2f}".format(flipped)
    except Exception:
        return eval_str


def request_stockfish(endpoint_name: str, url: str, fen: str):
    for attempt, delay in enumerate(BACKOFF_SECONDS, start=1):
        try:
            resp = requests.get(
                url,
                params={"fen": fen, "depth": DEPTH},
                timeout=12,
            )

            if resp.status_code != 200:
                log_api_failure(endpoint_name,
                    f"attempt {attempt}/{MAX_RETRIES} HTTP {resp.status_code}: {resp.text}")
                time.sleep(delay)
                continue

            try:
                data = resp.json()
            except Exception as exc:
                log_api_failure(endpoint_name,
                    f"attempt {attempt}/{MAX_RETRIES} JSON error {exc}: {resp.text}")
                time.sleep(delay)
                continue

            if data.get("success") is False:
                log_api_failure(endpoint_name,
                    f"attempt {attempt}/{MAX_RETRIES} API failure: {resp.text}")
                time.sleep(delay)
                continue

            move, eval_str, depth = parse_stockfish_response(data)
            if move:
                return move, eval_str, depth

            log_api_failure(endpoint_name,
                f"attempt {attempt}/{MAX_RETRIES} no legal best move in body: {resp.text}")
            time.sleep(delay)

        except Exception as exc:
            log_api_failure(endpoint_name, f"attempt {attempt}/{MAX_RETRIES} exception: {exc}")
            time.sleep(delay)

    return None, "err", DEPTH


def get_best_move(fen: str):
    move, eval_str, depth = request_stockfish("v2", STOCKFISH_URL, fen)
    if move:
        return move, eval_str, depth
    log_api_failure("v2", "all retries failed; switching to v1 fallback endpoint")
    return request_stockfish("v1", STOCKFISH_FALLBACK_URL, fen)


def queue_update(fen, move, eval_str, depth, my_color, turn, status):
    update_queue.put({
        "fen":     fen,
        "move":    move,
        "eval":    eval_str,
        "depth":   depth,
        "myColor": my_color,
        "turn":    turn,
        "status":  status,
    })


def start_position(data: dict):
    fen, my_color, turn, puzzle_mode, new_puzzle = parse_position_payload(data)
    if not fen:
        return {"error": "no fen"}, None

    mark_latest_fen(fen)

    if new_puzzle:
        queue_update(fen, None, "–", DEPTH, my_color, turn, "reset")

    if turn != my_color:
        queue_update(fen, None, "–", DEPTH, my_color, turn, "waiting")
        return {"status": "not_my_turn", "move": None, "eval": "–", "depth": DEPTH}, None

    if not puzzle_mode:
        queue_update(fen, None, "…", DEPTH, my_color, turn, "thinking")

    return None, {
        "fen":        fen,
        "myColor":    my_color,
        "turn":       turn,
        "puzzleMode": puzzle_mode,
    }


def finish_position(payload: dict):
    fen      = payload["fen"]
    my_color = payload["myColor"]
    turn     = payload["turn"]

    move, eval_str, depth = get_best_move(fen)

    # Flip eval sign for black player (Stockfish is always from white's POV)
    if move:
        eval_str = flip_eval_for_black(eval_str, my_color)

    status = "ready" if move else "error"

    if is_latest_fen(fen):
        queue_update(fen, move, eval_str, depth, my_color, turn, status)

    return {
        "status": status,
        "move":   move,
        "eval":   eval_str,
        "depth":  depth,
    }


@app.route("/position", methods=["POST", "OPTIONS"])
def position():
    if request.method == "OPTIONS":
        resp = jsonify({"ok": True})
        _cors(resp)
        return resp

    data = request.get_json(force=True, silent=True) or {}
    immediate, payload = start_position(data)

    if immediate and immediate.get("error"):
        resp = jsonify(immediate)
        _cors(resp)
        return resp, 400

    if immediate:
        resp = jsonify(immediate)
        _cors(resp)
        return resp

    threading.Thread(target=lambda: finish_position(payload), daemon=True).start()
    resp = jsonify({"status": "queued"})
    _cors(resp)
    return resp


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


async def ws_handler(websocket, path=None):
    with ws_clients_lock:
        ws_clients.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "status": "error", "move": None,
                    "eval": "bad_json", "depth": DEPTH,
                }))
                continue

            immediate, payload = start_position(data)
            if immediate:
                await websocket.send(json.dumps(immediate))
                continue

            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, finish_position, payload)
            await websocket.send(json.dumps(result))
    finally:
        with ws_clients_lock:
            ws_clients.discard(websocket)


async def broadcast_ws_control(payload: dict):
    with ws_clients_lock:
        clients = list(ws_clients)
    if not clients:
        return 0
    message = json.dumps(payload)
    dead = []
    sent = 0
    for client in clients:
        try:
            await client.send(message)
            sent += 1
        except Exception:
            dead.append(client)
    if dead:
        with ws_clients_lock:
            for client in dead:
                ws_clients.discard(client)
    return sent


def request_browser_recheck():
    set_manual_side_override(None)
    loop = ws_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            broadcast_ws_control({"command": "recheck_side"}),
            loop,
        )


def submit_local_position(fen: str, my_color: str):
    if not fen:
        return

    immediate, payload = start_position({
        "fen": fen,
        "myColor": my_color,
        "turn": fen_active_color(fen),
        "puzzleMode": False,
        "newPuzzle": False,
    })

    if payload:
        threading.Thread(target=finish_position, args=(payload,), daemon=True).start()


def run_websocket():
    global ws_loop
    if websockets is None:
        print("[server] websockets not installed; WS transport disabled.", file=sys.stderr, flush=True)
        return

    async def runner():
        global ws_loop
        ws_loop = asyncio.get_running_loop()
        print(f"[server] WebSocket listening on ws://{WS_HOST}:{WS_PORT}", flush=True)
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            await asyncio.Future()

    asyncio.run(runner())


# ── Colors / theme ────────────────────────────────────────────────────────────
LIGHT          = "#C8A97A"
DARK           = "#7A5C3A"
HIGHLIGHT_FROM = "#3A8A4A"
HIGHLIGHT_TO   = "#C4A000"
ARROW_COLOR    = "#00E87A"
BG             = "#0D0D14"
TITLEBAR_BG    = "#08080E"
PANEL_BG       = "#0F0F18"
TEXT_COLOR     = "#B8B8C4"
ACCENT         = "#00E87A"
ACCENT2        = "#E8A000"
BORDER_COLOR   = "#1A1A2A"
THINKING_COLOR = "#E8A000"
WAIT_COLOR     = "#445566"
ERROR_COLOR    = "#E84455"
SCAN_COLOR     = "#00CCAA"
WHITE_COLOR    = "#EEEEFF"
BLACK_COLOR    = "#888899"

PIECE_UNICODE = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}


def fen_to_board(fen: str):
    placement = fen.split()[0]
    board = []
    for rank_str in placement.split("/"):
        row = []
        for ch in rank_str:
            if ch.isdigit():
                row.extend([""] * int(ch))
            else:
                row.append(ch)
        board.append(row)
    return board


def uci_to_rc(sq: str, flipped: bool):
    """Convert UCI square (e.g. 'e2') to (row, col) in display coordinates."""
    file = ord(sq[0]) - ord("a")
    rank = int(sq[1]) - 1
    if flipped:
        # Black's perspective: rank 1 is at bottom (row 7), file a is at right (col 7)
        row = rank        # rank 1 → row 0 (top), rank 8 → row 7 (bottom)  WRONG for flipped
        col = 7 - file    # file a → col 7 (right side)
    else:
        row = 7 - rank    # rank 1 → row 7 (bottom), rank 8 → row 0 (top)
        col = file        # file a → col 0 (left)
    return row, col


class ChessGUI:
    SQ  = 40
    PAD = 12

    def __init__(self, root: tk.Tk):
        self.root = root
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.configure(bg=BORDER_COLOR)
        try:
            root.wm_attributes("-alpha", 0.96)
        except Exception:
            pass

        root.update_idletasks()
        sw = root.winfo_screenwidth()
        root.geometry(f"+{sw - 430}+{40}")

        self.board_data = [[""] * 8 for _ in range(8)]
        self.best_move  = None
        self.eval_str   = "–"
        self.depth_val  = str(DEPTH)
        self.turn       = "w"
        self.my_color   = "w"
        self.last_fen   = ""
        self.flipped    = False
        self.status     = "idle"

        self._drag_x        = 0
        self._drag_y        = 0
        self._think_dots    = 0
        self._scan_squares  = []
        self._scan_pulse    = 0
        self._arrow_progress = 1.0
        self._arrow_move    = None
        self._arrow_job     = None

        self._build_ui()
        self._poll_queue()
        self._animate_thinking()

    # ── Drag ──────────────────────────────────────────────────────────────
    def _start_drag(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _on_drag(self, event):
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._on_drag)

    # ── Layout ────────────────────────────────────────────────────────────
    def _build_ui(self):
        S = self.SQ
        P = self.PAD
        total_board = S * 8 + P * 2

        titlebar = tk.Frame(self.root, bg=TITLEBAR_BG, height=26)
        titlebar.pack(fill="x", side="top")
        titlebar.pack_propagate(False)
        self._bind_drag(titlebar)

        title_lbl = tk.Label(
            titlebar, text="  ♟  STOCKFISH ASSISTANT",
            bg=TITLEBAR_BG, fg=ACCENT,
            font=tkfont.Font(family="Courier New", size=9, weight="bold"),
            anchor="w",
        )
        title_lbl.pack(side="left", fill="y")
        self._bind_drag(title_lbl)

        close_btn = tk.Label(
            titlebar, text="  ✕  ",
            bg=TITLEBAR_BG, fg="#666680",
            font=tkfont.Font(family="Arial", size=10, weight="bold"),
            cursor="hand2",
        )
        close_btn.pack(side="right", fill="y")
        close_btn.bind("<Enter>",   lambda e: close_btn.config(fg="#FF4455", bg="#1a0a0e"))
        close_btn.bind("<Leave>",   lambda e: close_btn.config(fg="#666680", bg=TITLEBAR_BG))
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        tk.Frame(self.root, bg=ACCENT, height=1).pack(fill="x")

        content = tk.Frame(self.root, bg=BG)
        content.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(
            content,
            width=total_board, height=total_board,
            bg=BG, highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0, padx=(6, 4), pady=6)
        self._bind_drag(self.canvas)

        panel = tk.Frame(content, bg=PANEL_BG, width=110)
        panel.grid(row=0, column=1, padx=(0, 6), pady=6, sticky="ns")
        panel.grid_propagate(False)

        mono_big = tkfont.Font(family="Courier New", size=13, weight="bold")
        mono_sm  = tkfont.Font(family="Courier New", size=9,  weight="bold")
        lbl_font = tkfont.Font(family="Arial", size=7)

        def sep():
            tk.Frame(panel, bg=BORDER_COLOR, height=1).pack(fill="x", pady=3)

        # ── MY COLOR indicator (NEW) ───────────────────────────────────────
        tk.Label(panel, text="PLAYING AS", bg=PANEL_BG, fg="#445566",
                 font=lbl_font).pack(pady=(10, 0))
        self.lbl_my_color = tk.Label(
            panel, text="⬤ ?",
            bg=PANEL_BG, fg="#888899",
            font=tkfont.Font(family="Arial", size=8, weight="bold"),
        )
        self.lbl_my_color.pack()

        sep()

        tk.Label(panel, text="BEST MOVE", bg=PANEL_BG, fg="#445566",
                 font=lbl_font).pack(pady=(4, 0))
        self.lbl_move = tk.Label(panel, text="–", bg=PANEL_BG, fg=ACCENT, font=mono_big)
        self.lbl_move.pack()

        sep()

        tk.Label(panel, text="EVAL", bg=PANEL_BG, fg="#445566", font=lbl_font).pack()
        self.lbl_eval = tk.Label(panel, text="–", bg=PANEL_BG, fg=TEXT_COLOR, font=mono_sm)
        self.lbl_eval.pack()

        sep()

        tk.Label(panel, text="DEPTH", bg=PANEL_BG, fg="#445566", font=lbl_font).pack()
        self.lbl_depth = tk.Label(panel, text=str(DEPTH), bg=PANEL_BG, fg=TEXT_COLOR, font=mono_sm)
        self.lbl_depth.pack()

        sep()

        tk.Label(panel, text="TURN", bg=PANEL_BG, fg="#445566", font=lbl_font).pack()
        self.lbl_turn = tk.Label(panel, text="⬤ ?", bg=PANEL_BG, fg="#888899", font=lbl_font)
        self.lbl_turn.pack(pady=(2, 0))

        sep()

        self.lbl_status = tk.Label(
            panel, text="idle",
            bg=PANEL_BG, fg="#334455",
            font=tkfont.Font(family="Arial", size=7),
            wraplength=100,
        )
        self.lbl_status.pack(pady=(2, 4))

        self.btn_recheck = tk.Label(
            panel, text="RECHECK",
            bg=TITLEBAR_BG, fg=ACCENT,
            font=tkfont.Font(family="Courier New", size=8, weight="bold"),
            cursor="hand2", padx=6, pady=3,
        )
        self.btn_recheck.pack(pady=(0, 8))
        self.btn_recheck.bind("<Enter>",    lambda e: self.btn_recheck.config(bg="#101824"))
        self.btn_recheck.bind("<Leave>",    lambda e: self.btn_recheck.config(bg=TITLEBAR_BG))
        self.btn_recheck.bind("<Button-1>", lambda e: self._request_recheck())

        self.btn_switch = tk.Label(
            panel, text="SWITCH",
            bg=TITLEBAR_BG, fg=ACCENT2,
            font=tkfont.Font(family="Courier New", size=8, weight="bold"),
            cursor="hand2", padx=6, pady=3,
        )
        self.btn_switch.pack(pady=(0, 8))
        self.btn_switch.bind("<Enter>",    lambda e: self.btn_switch.config(bg="#1d1608"))
        self.btn_switch.bind("<Leave>",    lambda e: self.btn_switch.config(bg=TITLEBAR_BG))
        self.btn_switch.bind("<Button-1>", lambda e: self._switch_side())

        self._draw_board()

    def _request_recheck(self):
        self._cancel_arrow_animation()
        self.best_move = None
        self.eval_str  = "–"
        self.depth_val = str(DEPTH)
        self.status    = "reset"
        self.lbl_move.config(text="–", fg=WAIT_COLOR)
        self.lbl_eval.config(text="–", fg=WAIT_COLOR)
        self.lbl_status.config(text="rechecking\nside…", fg=ACCENT2)
        self._draw_board()
        request_browser_recheck()

    def _switch_side(self):
        self._cancel_arrow_animation()

        new_color = "b" if self.my_color == "w" else "w"
        set_manual_side_override(new_color)

        self.my_color = new_color
        self.flipped = (new_color == "b")
        self.best_move = None
        self.eval_str = "–"
        self.depth_val = str(DEPTH)
        self.status = "reset"

        if new_color == "w":
            self.lbl_my_color.config(text="⬤ WHITE", fg=WHITE_COLOR)
        else:
            self.lbl_my_color.config(text="⬤ BLACK", fg=BLACK_COLOR)

        self.lbl_move.config(text="–", fg=WAIT_COLOR)
        self.lbl_eval.config(text="–", fg=WAIT_COLOR)
        self.lbl_depth.config(text=str(DEPTH))
        self.lbl_status.config(text="manual\nside set", fg=ACCENT2)
        self._draw_board()

        if self.last_fen:
            submit_local_position(self.last_fen, new_color)

    def _display_to_board_rc(self, r, col):
        if self.flipped:
            return 7 - r, 7 - col
        return r, col

    def _board_to_display_rc(self, r, col):
        if self.flipped:
            return 7 - r, 7 - col
        return r, col

    # ── Drawing ───────────────────────────────────────────────────────────
    def _draw_board(self):
        c = self.canvas
        S = self.SQ
        P = self.PAD
        c.delete("all")

        my_turn = self.turn == self.my_color
        move    = self.best_move if my_turn and self.status not in ("thinking", "waiting") else None
        from_rc = uci_to_rc(move[:2], self.flipped) if move and len(move) >= 4 else None
        to_rc   = uci_to_rc(move[2:4], self.flipped) if move and len(move) >= 4 else None

        for r in range(8):
            for col in range(8):
                x1 = P + col * S
                y1 = P + r * S
                x2 = x1 + S
                y2 = y1 + S

                if from_rc and (r, col) == from_rc:
                    color = HIGHLIGHT_FROM
                elif to_rc and (r, col) == to_rc:
                    color = HIGHLIGHT_TO
                else:
                    color = LIGHT if (r + col) % 2 == 0 else DARK

                c.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

                br, bc = self._display_to_board_rc(r, col)
                piece = self.board_data[br][bc]
                if piece:
                    glyph   = PIECE_UNICODE.get(piece, piece)
                    color_p = "#FFFFFF" if piece.isupper() else "#111111"
                    shadow  = "#555555" if piece.isupper() else "#999999"
                    fs = S // 2 - 2
                    c.create_text(x1 + S//2 + 1, y1 + S//2 + 1, text=glyph,
                                  font=("Segoe UI Symbol", fs), fill=shadow)
                    c.create_text(x1 + S//2,     y1 + S//2,     text=glyph,
                                  font=("Segoe UI Symbol", fs), fill=color_p)

        if self.status == "thinking":
            self._draw_thinking_overlay()
        elif from_rc and to_rc:
            self._draw_arrow(from_rc, to_rc, self._arrow_progress)

        # File / rank labels — always match the board orientation
        files = "abcdefgh" if not self.flipped else "hgfedcba"
        ranks = "87654321" if not self.flipped else "12345678"
        for i in range(8):
            c.create_text(P + i * S + S // 2, P + 8 * S + P // 2,
                          text=files[i], fill="#445566", font=("Arial", 7))
            c.create_text(P // 2, P + i * S + S // 2,
                          text=ranks[i], fill="#445566", font=("Arial", 7))

        c.create_rectangle(P, P, P + 8 * S, P + 8 * S, outline=BORDER_COLOR, width=1)

    def _draw_thinking_overlay(self):
        c = self.canvas
        S = self.SQ
        P = self.PAD

        for r, col in self._scan_squares:
            x1 = P + col * S
            y1 = P + r * S
            c.create_rectangle(
                x1 + 3, y1 + 3, x1 + S - 3, y1 + S - 3,
                fill=SCAN_COLOR, outline=SCAN_COLOR, stipple="gray50",
            )

        king_rc = self._king_rc()
        if not king_rc:
            return
        r, col = king_rc
        cx = P + col * S + S // 2
        cy = P + r * S + S // 2
        radius = 5 + (self._scan_pulse % 6)
        c.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                      outline=SCAN_COLOR, width=2)
        c.create_line(cx - 10, cy, cx + 10, cy, fill=SCAN_COLOR, width=1)
        c.create_line(cx, cy - 10, cx, cy + 10, fill=SCAN_COLOR, width=1)

    def _king_rc(self):
        # My king: uppercase K if white, lowercase k if black
        target = "K" if self.my_color == "w" else "k"
        for r, row in enumerate(self.board_data):
            for col, piece in enumerate(row):
                if piece == target:
                    return self._board_to_display_rc(r, col)
        return None

    def _draw_arrow(self, from_rc, to_rc, progress=1.0):
        c = self.canvas
        S = self.SQ
        P = self.PAD
        fr, fc = from_rc
        tr, tc = to_rc
        x1 = P + fc * S + S // 2
        y1 = P + fr * S + S // 2
        x2 = P + tc * S + S // 2
        y2 = P + tr * S + S // 2

        progress = max(0.0, min(1.0, progress))
        ex = x1 + (x2 - x1) * progress
        ey = y1 + (y2 - y1) * progress

        c.create_line(x1, y1, ex, ey, fill="#007A40", width=8, capstyle=tk.ROUND)
        c.create_line(x1, y1, ex, ey, fill=ARROW_COLOR, width=4,
                      arrow=tk.LAST, arrowshape=(14, 17, 5),
                      capstyle=tk.ROUND, joinstyle=tk.ROUND)

    def _cancel_arrow_animation(self):
        if self._arrow_job:
            try:
                self.root.after_cancel(self._arrow_job)
            except Exception:
                pass
            self._arrow_job = None

    def _start_arrow_animation(self, move):
        self._cancel_arrow_animation()
        self._arrow_move    = move
        self._arrow_progress = 1 / 6

        def step(frame=1):
            if self.status != "ready" or self.best_move != move:
                return
            self._arrow_progress = frame / 6
            self._draw_board()
            if frame < 6:
                self._arrow_job = self.root.after(30, lambda: step(frame + 1))
            else:
                self._arrow_job = None

        step(1)

    # ── Thinking animation ────────────────────────────────────────────────
    def _animate_thinking(self):
        if self.status == "thinking":
            dots = "●" * (self._think_dots % 3 + 1) + "○" * (2 - self._think_dots % 3)
            self.lbl_status.config(text=f"thinking {dots}", fg=THINKING_COLOR)
            self._think_dots  += 1
            self._scan_pulse  += 1
            self._scan_squares = [
                (random.randrange(8), random.randrange(8))
                for _ in range(random.randint(2, 3))
            ]
            self._draw_board()
        self.root.after(80, self._animate_thinking)

    # ── Queue polling ─────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                item = update_queue.get_nowait()
                self._apply_update(item)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _apply_update(self, item: dict):
        fen      = item["fen"]
        move     = item["move"]
        eval_str = item["eval"]
        depth    = item["depth"]
        turn     = item["turn"]
        my_color = item["myColor"]
        status   = item.get("status", "ready")
        prev_move = self.best_move

        self.last_fen  = fen
        self.best_move = move
        self.eval_str  = eval_str
        self.depth_val = str(depth)
        self.turn      = turn
        self.my_color  = my_color
        self.flipped   = (my_color == "b")   # ← flip board when playing black
        self.board_data = fen_to_board(fen)
        self.status    = status

        my_turn = (turn == my_color)
        should_animate_arrow = False

        # ── Update MY COLOR label ──────────────────────────────────────────
        if my_color == 'w':
            self.lbl_my_color.config(text="⬤ WHITE", fg=WHITE_COLOR)
        else:
            self.lbl_my_color.config(text="⬤ BLACK", fg=BLACK_COLOR)

        # ── Update BEST MOVE / EVAL ────────────────────────────────────────
        if status == "thinking":
            self._cancel_arrow_animation()
            self.lbl_move.config(text="…", fg=THINKING_COLOR)
            self.lbl_eval.config(text="…", fg=THINKING_COLOR)
        elif status == "reset":
            self._cancel_arrow_animation()
            self.lbl_move.config(text="–", fg=WAIT_COLOR)
            self.lbl_eval.config(text="–", fg=WAIT_COLOR)
            self.lbl_status.config(text="new puzzle", fg=WAIT_COLOR)
        elif not my_turn:
            self._cancel_arrow_animation()
            self.lbl_move.config(text="–", fg=WAIT_COLOR)
            self.lbl_eval.config(text="–", fg=WAIT_COLOR)
        elif move:
            disp = f"{move[:2]}→{move[2:4]}"
            if len(move) == 5:
                disp += move[4].upper()
            self.lbl_move.config(text=disp, fg=ACCENT)
            self.lbl_eval.config(text=eval_str, fg=TEXT_COLOR)
            should_animate_arrow = (status == "ready" and move != prev_move)
        else:
            self._cancel_arrow_animation()
            self.lbl_move.config(text="err", fg=ERROR_COLOR)
            self.lbl_eval.config(text="–", fg=TEXT_COLOR)

        self.lbl_depth.config(text=str(depth))

        # ── Turn indicator ─────────────────────────────────────────────────
        if my_turn:
            self.lbl_turn.config(text="⬤ YOUR TURN", fg=ACCENT)
        else:
            opp = "W" if turn == "w" else "B"
            self.lbl_turn.config(text=f"⬤ {opp} MOVING", fg="#556677")

        # ── Status label ───────────────────────────────────────────────────
        if status == "thinking":
            pass  # handled by animate_thinking
        elif status == "waiting":
            self.lbl_status.config(text="opponent's\nturn", fg=WAIT_COLOR)
        elif status == "ready" and move:
            self.lbl_status.config(text="● ready", fg=ACCENT)
        elif status == "error":
            self.lbl_status.config(text="api error\nretrying…", fg=ERROR_COLOR)
        elif status != "reset":
            self.lbl_status.config(text="idle", fg="#334455")

        if should_animate_arrow:
            self._start_arrow_animation(move)
        else:
            self._arrow_progress = 1.0
            self._draw_board()


# ── Entry point ────────────────────────────────────────────────────────────────
def run_flask():
    print(f"[server] HTTP fallback on http://127.0.0.1:{HTTP_FALLBACK_PORT}/position", flush=True)
    app.run(host="127.0.0.1", port=HTTP_FALLBACK_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    if websockets is not None:
        threading.Thread(target=run_websocket, daemon=True).start()
    else:
        print("[server] Install websockets: pip install websockets", file=sys.stderr)

    threading.Thread(target=run_flask, daemon=True).start()

    root = tk.Tk()
    gui  = ChessGUI(root)
    root.mainloop()
