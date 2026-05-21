// ==UserScript==
// @name         Chess.com → Local Engine Bridge (WebSocket)
// @namespace    http://tampermonkey.net/
// @version      5.0
// @description  Sends FEN + player info to local Python chess engine server. Correctly detects color on every new game.
// @author       ChessBridge
// @match        *://www.chess.com/*
// @match        *://chess.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-start
// @license      MIT
// ==/UserScript==

(function () {
    'use strict';

    const WS_SERVER   = 'ws://127.0.0.1:5174';
    const HTTP_SERVER = 'http://127.0.0.1:5175/position';
    const POLL_MS     = 20;
    const MIN_SEND_MS = 20;
    const DEBUG       = false;

    const log = (...a) => DEBUG && console.log('[ChessBridge]', ...a);

    // ── Page detection ─────────────────────────────────────────────────────
    const GAME_PATTERNS = [
        /\/game\/live\//,
        /\/game\/daily\//,
        /\/game\/\d+/,
        /\/play\/computer/,
        /\/play\/online/,
        /\/analysis/,
        /\/puzzles/,
    ];
    const isGamePage   = () => GAME_PATTERNS.some(p => p.test(location.pathname));
    const isPuzzlePage = () => location.pathname.includes('/puzzles');

    // ── Board element ──────────────────────────────────────────────────────
    const getBoard = () =>
        document.querySelector('wc-chess-board') ||
        document.querySelector('chess-board')    ||
        null;

    // ── FEN validation ─────────────────────────────────────────────────────
    const isValidFen = (fen) => {
        if (!fen || typeof fen !== 'string') return false;
        const p = fen.trim().split(/\s+/);
        if (p.length < 2) return false;
        const ranks = p[0].split('/');
        if (ranks.length !== 8) return false;
        for (const rank of ranks) {
            let n = 0;
            for (const c of rank) {
                if ('rnbqkpRNBQKP'.includes(c)) n++;
                else if ('12345678'.includes(c)) n += +c;
                else return false;
            }
            if (n !== 8) return false;
        }
        return true;
    };

    // ── FEN extraction ─────────────────────────────────────────────────────
    const getFen = () => {
        const b = getBoard();
        if (!b) return null;

        try {
            if (b.game?.getFEN) {
                const fen = b.game.getFEN();
                if (isValidFen(fen)) return fen;
            }
        } catch (_) {}
        try {
            if (b.game?.fen) {
                const fen = typeof b.game.fen === 'function' ? b.game.fen() : b.game.fen;
                if (isValidFen(fen)) return fen;
            }
        } catch (_) {}
        try {
            const g = b._game || b.game?._game;
            if (g?.getFEN) {
                const fen = g.getFEN();
                if (isValidFen(fen)) return fen;
            }
        } catch (_) {}
        try {
            const ctrl = b.gameController || b._gameController;
            if (ctrl?.getPosition) {
                const pos = ctrl.getPosition();
                if (isValidFen(pos)) return pos;
            }
        } catch (_) {}
        try {
            const el = document.querySelector('[data-fen]');
            if (el) {
                const fen = el.getAttribute('data-fen');
                if (isValidFen(fen)) return fen;
            }
        } catch (_) {}
        try {
            const store = window.__chessStore || window.chessStore;
            if (store?.getState) {
                const state = store.getState();
                const fen   = state?.game?.fen || state?.currentGame?.fen;
                if (isValidFen(fen)) return fen;
            }
        } catch (_) {}

        return null;
    };

    // ── Game identity tracking ─────────────────────────────────────────────
    // We track a "game signature" = the combination of URL + the two player names.
    // Whenever this changes, we FULLY wipe the color cache and re-detect.
    let currentGameSig = '';

    const getGameSignature = () => {
        // Use URL path as primary key (changes on every new game)
        const urlPart = location.pathname;

        // Also grab player names as secondary key (catches same-URL replays)
        let players = '';
        try {
            const nameEls = Array.from(document.querySelectorAll(
                '.player-tagline-username, [data-testid="user-tagline-username"], [data-testid*="user-tagline-username"]'
            )).map(el => el.textContent.trim()).filter(Boolean);
            players = nameEls.sort().join('|');
        } catch (_) {}

        return `${urlPart}::${players}`;
    };

    const checkForNewGame = () => {
        const sig = getGameSignature();
        if (sig && sig !== currentGameSig) {
            if (currentGameSig !== '') {
                log('New game detected, wiping color cache. Old:', currentGameSig, '→ New:', sig);
                fullColorReset();
            }
            currentGameSig = sig;
        }
    };

    const fullColorReset = () => {
        cachedColor      = '';
        cachedColorAt    = 0;
        cachedUsername   = '';
        cachedUsernameAt = 0;
        lastSentFen      = '';
        lastObservedFen  = '';
        lastSentAt       = 0;
        colorConfirmedCount = 0;
        confirmedColor   = '';
    };

    // ── Color confidence system ────────────────────────────────────────────
    // We don't trust ANY single color reading. We require N consistent readings
    // before locking in a color for the game. This prevents stale DOM reads.
    let cachedColor         = '';
    let cachedColorAt       = 0;
    let cachedUsername      = '';
    let cachedUsernameAt    = 0;
    let colorConfirmedCount = 0;
    let confirmedColor      = '';   // locked-in color after enough consistent reads
    const COLOR_CONFIRM_NEEDED = 3; // require 3 consistent reads to lock in

    const normalizeName = (name) => String(name || '').trim().replace(/^@/, '').toLowerCase();
    const opposite      = (c)    => c === 'b' ? 'w' : 'b';

    // ── Method 1: CSS square classes (most reliable) ───────────────────────
    // Chess.com piece divs have classes like "piece wp square-52" where
    // square-FC means File=F (1=a … 8=h), Rank=C (1-8).
    // YOUR pieces are always on the bottom ranks.
    // If white pieces sit on ranks 1-2 → you are white.
    // If black pieces sit on ranks 1-2 → board is flipped → you are black.
    const colorByPieceSquares = () => {
        const b = getBoard();
        if (!b) return '';

        const pieces = Array.from(b.querySelectorAll('[class*="piece"]'));
        if (pieces.length < 10) return ''; // board not fully loaded yet

        const rank12White = [];  // white pieces on ranks 1-2
        const rank12Black = [];  // black pieces on ranks 1-2
        const rank78White = [];  // white pieces on ranks 7-8
        const rank78Black = [];  // black pieces on ranks 7-8

        for (const piece of pieces) {
            const cls = String(piece.className || '');
            // piece color: wp/wn/wb/wr/wq/wk or bp/bn/bb/br/bq/bk
            const cm = cls.match(/(?:^|\s)([wb])[pnbrqk](?:\s|$)/i);
            if (!cm) continue;
            const pc = cm[1].toLowerCase(); // 'w' or 'b'

            // square class: square-FC
            const sm = cls.match(/square-(\d)(\d)/);
            if (!sm) continue;
            const rank = parseInt(sm[2], 10);

            if (rank === 1 || rank === 2) {
                if (pc === 'w') rank12White.push(rank);
                else            rank12Black.push(rank);
            }
            if (rank === 7 || rank === 8) {
                if (pc === 'w') rank78White.push(rank);
                else            rank78Black.push(rank);
            }
        }

        // Standard: white on ranks 1-2 → player is white
        if (rank12White.length >= 4 && rank12Black.length === 0) return 'w';
        // Flipped: black on ranks 1-2 → board is from black's POV → player is black
        if (rank12Black.length >= 4 && rank12White.length === 0) return 'b';

        // Mixed (mid-game pieces captured): use majority
        if (rank12White.length > rank12Black.length + 1) return 'w';
        if (rank12Black.length > rank12White.length + 1) return 'b';

        // Fallback: check back ranks (ranks 7-8)
        if (rank78Black.length >= 4 && rank78White.length === 0) return 'w'; // black's back = top = you're white
        if (rank78White.length >= 4 && rank78Black.length === 0) return 'b'; // white's back = top = you're black

        return '';
    };

    // ── Method 2: Board orientation attribute ──────────────────────────────
    const colorByBoardAttr = () => {
        const b = getBoard();
        if (!b) return '';
        try {
            const attrs = ['board-orientation', 'orientation', 'data-orientation', 'flipped'];
            for (const attr of attrs) {
                const val = b.getAttribute(attr);
                if (val === 'black' || val === 'true' || val === '')  return 'b';
                if (val === 'white' || val === 'false') return 'w';
            }
            if (b.classList.contains('flipped') || b.classList.contains('black')) return 'b';
        } catch (_) {}
        return '';
    };

    // ── Method 3: Coordinate rank labels ──────────────────────────────────
    const colorByCoords = () => {
        const b = getBoard();
        if (!b) return '';
        try {
            const bRect = b.getBoundingClientRect();
            if (!bRect.width) return '';

            const isInsideBoard = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.width < 35 &&
                       r.left >= bRect.left - 35 && r.right  <= bRect.right  + 35 &&
                       r.top  >= bRect.top  - 10 && r.bottom <= bRect.bottom + 10;
            };

            // Try board-internal coordinate elements first
            let coords = Array.from(b.querySelectorAll('[class*="coordinate"], [class*="coord"], .coords'))
                .filter(el => /^[1-8]$/.test(el.textContent.trim()) && isInsideBoard(el));

            if (!coords.length) {
                // Broader search near the board
                coords = Array.from(document.querySelectorAll('*'))
                    .filter(el => /^[1-8]$/.test(el.textContent.trim()) && isInsideBoard(el));
            }
            if (coords.length < 4) return '';

            const sorted = coords
                .map(el => ({ v: el.textContent.trim(), top: el.getBoundingClientRect().top }))
                .sort((a, bEl) => bEl.top - a.top); // highest Y = visually bottom

            const bottom = sorted[0]?.v;
            const top    = sorted[sorted.length - 1]?.v;

            if (bottom === '1') return 'w';
            if (bottom === '8') return 'b';
            if (top    === '8') return 'w';
            if (top    === '1') return 'b';
        } catch (_) {}
        return '';
    };

    // ── Method 4: Player bar username matching ─────────────────────────────
    const findUsernameDeep = (obj, depth = 0, seen = new Set()) => {
        if (!obj || depth > 4 || seen.has(obj) || typeof obj !== 'object') return '';
        seen.add(obj);
        const wantedKeys = /^(username|userName|login|memberName|displayName|name)$/i;
        try {
            for (const [key, value] of Object.entries(obj)) {
                if (typeof value === 'string' && wantedKeys.test(key) && value.length >= 2 && value.length <= 40)
                    return value;
            }
            for (const value of Object.values(obj)) {
                const f = findUsernameDeep(value, depth + 1, seen);
                if (f) return f;
            }
        } catch (_) {}
        return '';
    };

    const getLoggedInUsername = () => {
        const now = performance.now();
        if (cachedUsername && now - cachedUsernameAt < 2000) return cachedUsername;

        for (const candidate of [
            window.__CHESSCOM_CONFIG__, window.__INITIAL_STATE__,
            window.__NEXT_DATA__, window.chesscom, window.ChessCom,
            window.chessConfig, window.user, window.app,
        ]) {
            const found = findUsernameDeep(candidate);
            if (found) { cachedUsername = found; cachedUsernameAt = now; return found; }
        }
        try {
            const el = document.querySelector(
                '[data-testid*="user"] [class*="username"], .user-username, .user-menu-username'
            );
            if (el?.textContent?.trim()) {
                cachedUsername = el.textContent.trim();
                cachedUsernameAt = now;
                return cachedUsername;
            }
        } catch (_) {}
        cachedUsernameAt = now;
        return '';
    };

    const colorByPlayerBars = () => {
        const loggedIn = normalizeName(getLoggedInUsername());
        if (!loggedIn) return '';

        const selector = [
            '.player-tagline-username',
            '[data-testid="user-tagline-username"]',
            '[data-testid*="user-tagline-username"]',
            '.player-component .username',
            '[class*="playerName"]',
            '[class*="player-name"]',
        ].join(',');

        const nodes = Array.from(document.querySelectorAll(selector))
            .filter(el => el?.textContent?.trim())
            .map(el => ({ name: el.textContent.trim(), rect: el.getBoundingClientRect() }))
            .filter(item => item.rect.width > 0 && item.rect.height > 0)
            .sort((a, bEl) => a.rect.top - bEl.rect.top);

        if (nodes.length < 2) return '';

        const topName    = normalizeName(nodes[0].name);
        const bottomName = normalizeName(nodes[nodes.length - 1].name);

        // Get board bottom color from other methods (don't recurse)
        const boardBottomColor = colorByPieceSquares() || colorByBoardAttr() || colorByCoords() || 'w';

        if (bottomName === loggedIn) return boardBottomColor;
        if (topName    === loggedIn) return opposite(boardBottomColor);
        return '';
    };

    // ── Method 5: Game API ─────────────────────────────────────────────────
    const colorByGameAPI = () => {
        const b = getBoard();
        if (!b) return '';
        try {
            if (typeof b.game?.getPlayingAs === 'function') {
                const pa = b.game.getPlayingAs();
                if (pa === 1 || pa === 'white') return 'w';
                if (pa === 2 || pa === 'black') return 'b';
            }
        } catch (_) {}
        try {
            const opts = b.game?.getOptions?.();
            if (opts?.flipped === true)  return 'b';
            if (opts?.flipped === false) return 'w';
        } catch (_) {}
        try {
            const m = location.href.match(/[?&]color=(white|black)/i);
            if (m) return m[1].toLowerCase() === 'black' ? 'b' : 'w';
        } catch (_) {}
        return '';
    };

    // ── Master color resolver with confidence locking ──────────────────────
    const getMyColor = () => {
        const now = performance.now();

        // If we have a confirmed (locked) color for this game, return it.
        // Only re-poll briefly every 500ms in case of rare mid-game corrections.
        if (confirmedColor && now - cachedColorAt < 500) return confirmedColor;

        const METHODS = [
            { name: 'pieceSquares', fn: colorByPieceSquares },
            { name: 'boardAttr',    fn: colorByBoardAttr   },
            { name: 'coordinates',  fn: colorByCoords      },
            { name: 'playerBars',   fn: colorByPlayerBars  },
            { name: 'gameAPI',      fn: colorByGameAPI     },
        ];

        let detected = '';
        for (const { name, fn } of METHODS) {
            try {
                const c = fn();
                if (c === 'w' || c === 'b') {
                    detected = c;
                    log(`Color detected via ${name}: ${c}`);
                    break;
                }
            } catch (e) {
                log(`Method ${name} threw:`, e);
            }
        }

        if (!detected) {
            log('All color methods failed, keeping', confirmedColor || cachedColor || 'unset');
            return confirmedColor || cachedColor || 'w';
        }

        // Confidence system: accumulate consistent readings
        if (detected === cachedColor) {
            colorConfirmedCount++;
        } else {
            // Color changed → restart confidence count
            log(`Color changed ${cachedColor} → ${detected}, resetting confidence`);
            colorConfirmedCount = 1;
            cachedColor = detected;
        }

        cachedColorAt = now;

        if (colorConfirmedCount >= COLOR_CONFIRM_NEEDED) {
            if (confirmedColor !== detected) {
                log(`Color LOCKED IN as ${detected} (confirmed ${colorConfirmedCount}x)`);
                confirmedColor = detected;
            }
        }

        return confirmedColor || cachedColor;
    };

    // ── Game-over detection ────────────────────────────────────────────────
    const isGameOver = () => {
        try {
            if (getBoard()?.game?.getPositionInfo?.()?.gameOver === true) return true;
        } catch (_) {}
        try {
            if (document.querySelector('.game-over-modal, .modal-game-over, [data-cy="game-result-modal"]'))
                return true;
        } catch (_) {}
        return false;
    };

    // ── Whose turn from FEN ────────────────────────────────────────────────
    const getTurnFromFen = (fen) => fen.trim().split(/\s+/)[1] === 'b' ? 'b' : 'w';

    // ── Puzzle reset detection ─────────────────────────────────────────────
    const expandedPlacement = (fen) => {
        const out = [];
        for (const ch of (fen.trim().split(/\s+/)[0] || '')) {
            if (ch === '/') continue;
            if ('12345678'.includes(ch)) for (let i = 0; i < +ch; i++) out.push('.');
            else out.push(ch);
        }
        return out;
    };

    const pieceCount     = (fen) => expandedPlacement(fen).filter(c => c !== '.').length;
    const changedSquares = (a, b) => {
        const aa = expandedPlacement(a), bb = expandedPlacement(b);
        let d = 0;
        for (let i = 0; i < 64; i++) if (aa[i] !== bb[i]) d++;
        return d;
    };
    const isNewPuzzleFen = (prev, next) => {
        if (!prev || !next || !isPuzzlePage()) return false;
        if (Math.abs(pieceCount(prev) - pieceCount(next)) >= 3) return true;
        return changedSquares(prev, next) >= 16;
    };

    // ── WebSocket + HTTP transport ─────────────────────────────────────────
    let socket        = null;
    let socketOpen    = false;
    let reconnectTimer = null;
    let reconnectDelay = 250;
    let httpInFlight  = false;

    const connectSocket = () => {
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
        try {
            socket = new WebSocket(WS_SERVER);
        } catch (err) {
            log('WebSocket create failed:', err);
            scheduleReconnect();
            return;
        }

        socket.addEventListener('open', () => {
            socketOpen    = true;
            reconnectDelay = 250;
            fullColorReset();
            setTimeout(poll, 0);
            log('WebSocket connected');
        });

        socket.addEventListener('message', (event) => {
            try {
                const msg = JSON.parse(event.data);
                log('Server response:', msg);
                if (msg.command === 'recheck_side') {
                    fullColorReset();
                    setTimeout(poll, 0);
                }
            } catch (_) {
                log('Server response (raw):', event.data);
            }
        });

        socket.addEventListener('close', () => {
            socketOpen = false;
            scheduleReconnect();
        });

        socket.addEventListener('error', () => {
            socketOpen = false;
            try { socket.close(); } catch (_) {}
        });
    };

    const scheduleReconnect = () => {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connectSocket();
        }, reconnectDelay);
        reconnectDelay = Math.min(5000, reconnectDelay * 2);
    };

    const sendHttpFallback = (payload) => {
        if (httpInFlight) return;
        httpInFlight = true;
        GM_xmlhttpRequest({
            method:  'POST',
            url:     HTTP_SERVER,
            headers: { 'Content-Type': 'application/json' },
            data:    JSON.stringify(payload),
            timeout: 6000,
            onerror:   () => { httpInFlight = false; },
            ontimeout: () => { httpInFlight = false; },
            onload:    () => { httpInFlight = false; },
        });
    };

    const sendPosition = (fen, myColor, turn, opts = {}) => {
        const payload = {
            fen,
            myColor,
            turn,
            puzzleMode: isPuzzlePage(),
            newPuzzle:  Boolean(opts.newPuzzle),
        };
        log(`→ FEN sent | turn=${turn} myColor=${myColor} confirmed=${confirmedColor} ws=${socketOpen}`);

        if (socketOpen && socket?.readyState === WebSocket.OPEN) {
            try { socket.send(JSON.stringify(payload)); return; }
            catch (err) { log('WS send failed, HTTP fallback:', err); }
        }
        connectSocket();
        sendHttpFallback(payload);
    };

    // ── Main poll loop ─────────────────────────────────────────────────────
    let lastSentFen     = '';
    let lastObservedFen = '';
    let lastSentAt      = 0;

    const poll = () => {
        if (!isGamePage()) return;
        if (isGameOver())  return;

        // Always check if we're in a new game first
        checkForNewGame();

        const fen = getFen();
        if (!fen) return;

        const now        = performance.now();
        const turn       = getTurnFromFen(fen);
        const myColor    = getMyColor();
        const fenChanged = fen !== lastSentFen;
        const newPuzzle  = isNewPuzzleFen(lastObservedFen, fen);

        lastObservedFen = fen;

        if (!fenChanged) return;
        if (!newPuzzle && now - lastSentAt < MIN_SEND_MS) return;

        lastSentFen = fen;
        lastSentAt  = now;

        sendPosition(fen, myColor, turn, { newPuzzle });
    };

    connectSocket();
    setInterval(poll, POLL_MS);

    // ── SPA navigation watcher (backup) ───────────────────────────────────
    let lastHref = location.href;
    setInterval(() => {
        if (location.href !== lastHref) {
            lastHref = location.href;
            fullColorReset();
            currentGameSig = '';
            log('URL change detected, full reset');
        }
    }, 300);

    // ── MutationObserver: catch chess.com SPA game swaps ──────────────────
    // Chess.com sometimes reuses the same URL but swaps the board element.
    // We watch for the board being replaced in the DOM.
    let lastBoardEl = null;
    const boardObserver = new MutationObserver(() => {
        const board = getBoard();
        if (board && board !== lastBoardEl) {
            log('Board element replaced, full reset');
            lastBoardEl = board;
            fullColorReset();
            currentGameSig = '';
            // Small delay to let new board render its pieces
            setTimeout(() => { checkForNewGame(); }, 300);
        }
    });

    // Start observing once DOM is ready
    const startObserver = () => {
        boardObserver.observe(document.body, { childList: true, subtree: true });
        lastBoardEl = getBoard();
    };

    if (document.body) {
        startObserver();
    } else {
        document.addEventListener('DOMContentLoaded', startObserver);
    }

    log('Chess Bridge v5.0 loaded');
})();