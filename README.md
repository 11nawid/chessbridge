# ♟ ChessBridge — Stockfish Assistant for Chess.com

**Real-time Stockfish engine overlay for Chess.com, powered by a local Python server and a Tampermonkey userscript.**

ChessBridge silently reads the live board position from Chess.com, sends it to a local Python server, queries the Stockfish engine, and displays the best move + evaluation in a sleek always-on-top GUI overlay — all in under a second.

---

## ✨ Features

- **Real-time FEN extraction** from Chess.com (live, daily, puzzles, computer games, and analysis)
- **5-method color detection** — reliably knows whether you're playing white or black using piece positions, board attributes, DOM coordinates, player bars, and the game API
- **Confidence-locking system** — requires 3 consistent color readings before committing, preventing wrong-side analysis
- **WebSocket transport** (primary) with **HTTP fallback** — near-zero latency, automatic reconnect
- **Stockfish API integration** via [stockfish.online](https://stockfish.online), with depth-15 analysis and automatic retry/backoff
- **Eval sign correction** — Stockfish always returns evaluation from White's perspective; ChessBridge flips it for Black players
- **Animated arrow overlay** on the mini-board showing the best move from→to
- **Always-on-top draggable GUI** — sits beside your browser, shows board, best move, eval, depth, turn indicator, and your color
- **RECHECK button** — forces browser-side re-detection of your color
- **SWITCH button** — manually override which side you're playing
- **Puzzle support** — detects new puzzle loads and resets cleanly
- **SPA navigation aware** — handles Chess.com's single-page app URL changes and board element swaps

---

## 📸 How It Looks

```
┌─────────────────────────────────────┐
│  ♟  STOCKFISH ASSISTANT          ✕  │
├─────────────────────────────────────┤
│                    │  PLAYING AS    │
│   [mini chessboard]│  ⬤ WHITE       │
│   with arrow       │                │
│   overlay          │  BEST MOVE     │
│                    │  e2→e4         │
│                    │                │
│                    │  EVAL          │
│                    │  +0.35         │
│                    │                │
│                    │  DEPTH   15    │
│                    │  TURN ⬤ YOURS  │
│                    │  ● ready       │
│                    │  [RECHECK]     │
│                    │  [SWITCH]      │
└─────────────────────────────────────┘
```

---

## 🗂 Project Structure

```
chessbridge/
├── chess_server.py          # Python server: WebSocket + HTTP + Tkinter GUI
├── chessbridge.user.js      # Tampermonkey userscript for Chess.com
├── requirements.txt         # Python dependencies
└── README.md
```

---

## ⚙️ Requirements

### Python (server)
- Python 3.8+
- `flask` — HTTP fallback endpoint
- `requests` — calls Stockfish API
- `websockets` — primary transport
- `tkinter` — GUI (usually bundled with Python; on Linux: `sudo apt install python3-tk`)

### Browser (userscript)
- [Tampermonkey](https://www.tampermonkey.net/) extension (Chrome, Firefox, Edge, Safari)

---

## 🚀 Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/chessbridge.git
cd chessbridge
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install the Tampermonkey userscript

1. Install the [Tampermonkey](https://www.tampermonkey.net/) browser extension.
2. Click the Tampermonkey icon → **Create a new script**.
3. Delete the default content and paste the entire contents of `chessbridge.user.js`.
4. Save (`Ctrl+S`).

### 4. Start the server

```bash
python chess_server.py
```

You should see:

```
[server] WebSocket listening on ws://127.0.0.1:5174
[server] HTTP fallback on http://127.0.0.1:5175/position
```

The GUI overlay window will appear in the top-right corner of your screen.

### 5. Open Chess.com and start a game

The overlay will automatically detect the position and begin showing suggestions.

---

## 🔌 How It Works

```
Chess.com (browser)
      │
      │  FEN + color + turn  (WebSocket ws://127.0.0.1:5174)
      │  fallback: HTTP POST  http://127.0.0.1:5175/position
      ▼
chess_server.py
      │
      ├── Validates & parses the FEN
      ├── Sanity-checks detected player color
      ├── Skips analysis on opponent's turn
      │
      │  GET ?fen=...&depth=15
      ▼
stockfish.online API
      │
      │  bestmove, eval, depth
      ▼
chess_server.py  ──►  Tkinter GUI overlay
                        (best move, eval, board, arrow)
```

### Communication flow in detail

1. **Userscript polls** the Chess.com DOM every 20ms looking for FEN changes.
2. On a change it sends a JSON payload over WebSocket:
   ```json
   { "fen": "rnbqkb1r/.../w KQkq - 0 1", "myColor": "w", "turn": "w", "puzzleMode": false }
   ```
3. The **Python server** validates the FEN, cross-checks the player color, and — if it's your turn — calls the Stockfish API.
4. The Stockfish response is parsed, the evaluation sign is flipped for Black players, and the result is pushed to the **Tkinter GUI queue**.
5. The GUI updates the board display, move label, eval, and animates the arrow.

---

## 🎨 Color Detection Methods

The userscript uses five independent methods and locks in a color after 3 consistent readings:

| # | Method | How |
|---|--------|-----|
| 1 | **Piece squares** | Reads CSS `square-FC` classes; if your pieces are on ranks 1–2, you're White |
| 2 | **Board attribute** | Checks `board-orientation` / `flipped` HTML attributes |
| 3 | **Coordinate labels** | Inspects rank-label DOM elements; if rank `1` is at the bottom, you're White |
| 4 | **Player bars** | Matches your logged-in username against the bottom player bar |
| 5 | **Game API** | Calls `board.game.getPlayingAs()` if available |

The server also runs a **sanity check**: if White's pieces appear on ranks 7–8 in the FEN, the board is flipped and the server corrects the color to Black automatically.

---

## 🛠 Configuration

All constants are at the top of each file and easy to edit.

### `chessbridge.user.js`

| Constant | Default | Description |
|----------|---------|-------------|
| `WS_SERVER` | `ws://127.0.0.1:5174` | WebSocket server address |
| `HTTP_SERVER` | `http://127.0.0.1:5175/position` | HTTP fallback address |
| `POLL_MS` | `20` | DOM polling interval (ms) |
| `MIN_SEND_MS` | `20` | Minimum time between sends (ms) |
| `DEBUG` | `false` | Set `true` to enable console logging |

### `chess_server.py`

| Constant | Default | Description |
|----------|---------|-------------|
| `WS_PORT` | `5174` | WebSocket port |
| `HTTP_FALLBACK_PORT` | `5175` | HTTP fallback port |
| `DEPTH` | `15` | Stockfish analysis depth |
| `MAX_RETRIES` | `5` | API retry attempts |

---

## 🩺 Troubleshooting

**Overlay shows wrong color (e.g., analyzing Black's moves as White)**
Press the **RECHECK** button — it resets color detection and tells the userscript to re-detect from scratch. If it keeps happening, use **SWITCH** to manually force the correct color.

**No suggestions appearing**
- Make sure `chess_server.py` is running.
- Check the Tampermonkey dashboard — the script should show as "enabled" on chess.com.
- Open DevTools → Console and look for `[ChessBridge]` logs (set `DEBUG = true` first).

**`websockets` module not found**
```bash
pip install websockets
```

**`tkinter` not found (Linux)**
```bash
sudo apt install python3-tk
```

**Stockfish API errors in terminal**
The free [stockfish.online](https://stockfish.online) API has rate limits. The server will retry automatically with exponential backoff. If errors persist, reduce analysis frequency by increasing `POLL_MS` in the userscript.

---

## 📦 Dependencies

```
flask>=2.3.0
requests>=2.31.0
websockets>=12.0
```

Tkinter is part of the Python standard library (install separately on some Linux distros).

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Credits

- Engine analysis powered by [stockfish.online](https://stockfish.online) (free public API)
- Piece Unicode glyphs from standard Unicode chess symbols
- Built with Python `tkinter`, `flask`, `websockets`, and the Tampermonkey userscript API

---

## 📬 Contact

Have a question, found a bug, or want to collaborate on something cool?

Feel free to reach out — always open to feedback, ideas, and collabs.

- **Instagram:** [@11.skibidi](https://www.instagram.com/11.skibidi)

---

## ⚠️ Disclaimer

This tool is intended for learning, analysis, and personal study. Using engine assistance in rated or competitive games violates Chess.com's Terms of Service. Use responsibly.