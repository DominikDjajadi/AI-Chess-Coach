/**
 * Play vs engine + analysis tab (Flask API).
 */
(function () {
  const PIECE_IMG = {
    K: "white-king.png",
    Q: "white-queen.png",
    R: "white-rook.png",
    B: "white-bishop.png",
    N: "white-knight.png",
    P: "white-pawn.png",
    k: "black-king.png",
    q: "black-queen.png",
    r: "black-rook.png",
    b: "black-bishop.png",
    n: "black-knight.png",
    p: "black-pawn.png",
  };

  const PALETTE_WHITE = ["K", "Q", "R", "B", "N", "P"];
  const PALETTE_BLACK = ["k", "q", "r", "b", "n", "p"];
  const TOOL_LABELS = {
    K: "White king", Q: "White queen", R: "White rook", B: "White bishop", N: "White knight", P: "White pawn",
    k: "Black king", q: "Black queen", r: "Black rook", b: "Black bishop", n: "Black knight", p: "Black pawn",
  };
  const CASTLE_KEYS = ["K", "Q", "k", "q"];

  const STRENGTH_SIMS = {
    fast: 32,
    balanced: 64,
    deep: 128,
    extreme: 256,
  };
  const DEFAULT_STRENGTH = "fast";
  const DEFAULT_ANALYSIS_SIMS = 32;
  const MIN_ANALYSIS_SIMS = 8;
  const MAX_ANALYSIS_SIMS = 8192;
  // Stockfish only honors UCI_Elo in 1320..3190 — values below 1320 are
  // silently ignored by the engine. The real range is refined on page load
  // from /api/health.play_elo_min/max so this stays in sync with whatever
  // Stockfish build is running.
  let PLAY_ELO_MIN = 1320;
  let PLAY_ELO_MAX = 2800;
  const ANALYSIS_TOP_K = 8;
  /** Collapse candidate list past this count unless the user expands. */
  const CANDIDATES_VISIBLE_DEFAULT = 5;
  /** Max plies in PV line (first move + continuations); UI select caps at this. */
  const MAX_PV_PLIES = 16;
  let playSessionId = null;
  let lastPlayPgn = "";
  let lastPlayFen = "";
  let playLegal = [];
  let playYourTurn = false;
  let selectedSquare = null;
  let prevMoveLen = 0;
  let serverMoves = [];
  let optimisticUci = null;
  let optimisticFenOverride = null;
  let playHighlightFrom = null;
  let playHighlightTo = null;
  let playEngineFlashUci = null;
  let humanColorChoice = "white";

  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

  let lastAnalyzeFen = START_FEN;
  let selectedCandidate = null;
  let analyzeEditMode = false;
  let analyzeEditGrid = null;
  /** Active editor tool: { char: "K" } for placement, { eraser: true } for
   * erase, or null for neutral / Move pieces. Sticky — a placement does not
   * auto-clear the tool. */
  let analyzeHand = null;
  /** Snapshot of {fen, turn, castling} captured when entering edit mode, so Cancel can revert. */
  let editorSnapshot = null;
  /** Undo stack of editor snapshots; pushed before any board-changing action. */
  let editorUndoStack = [];
  /** In-flight drag-to-move source: { sq, cell } or null. */
  let editorDragSrc = null;
  /** Max undo depth — plenty for typical editing flows. */
  const EDITOR_UNDO_MAX = 64;
  /** Cancel stale in-flight analyze/PV when the user triggers a new one. */
  let analyzeFetchController = null;
  let pvFetchController = null;
  let explainFetchController = null;
  /** Avoid PATCH loop when syncing engine Elo from server state. */
  let playEloProgrammatic = false;

  function el(id) {
    return document.getElementById(id);
  }

  function parseFenBoard(fen) {
    const placement = fen.split(" ")[0];
    const rows = placement.split("/");
    const grid = Array(8)
      .fill(null)
      .map(() => Array(8).fill(null));
    for (let r = 0; r < 8; r++) {
      let c = 0;
      for (const ch of rows[r]) {
        if (/\d/.test(ch)) {
          c += parseInt(ch, 10);
        } else {
          grid[r][c] = ch;
          c += 1;
        }
      }
    }
    return grid;
  }

  function cloneGrid(grid) {
    return grid.map((row) => row.slice());
  }

  function gridToPlacement(grid) {
    const rows = [];
    for (let r = 0; r < 8; r++) {
      let s = "";
      let empty = 0;
      for (let c = 0; c < 8; c++) {
        const ch = grid[r][c];
        if (!ch) {
          empty++;
          continue;
        }
        if (empty) {
          s += empty;
          empty = 0;
        }
        s += ch;
      }
      if (empty) s += empty;
      rows.push(s || "8");
    }
    return rows.join("/");
  }

  function buildFenFromGrid(grid, turn, castling) {
    const c = castling && castling.length ? castling : "-";
    return `${gridToPlacement(grid)} ${turn} ${c} - 0 1`;
  }

  function parseCastlingFromFen(fen) {
    const parts = (fen || "").split(/\s+/);
    const raw = parts[2] || "-";
    if (!raw || raw === "-") return "-";
    let out = "";
    CASTLE_KEYS.forEach((k) => {
      if (raw.includes(k)) out += k;
    });
    return out || "-";
  }

  function uciFromCoords(file, rank) {
    return String.fromCharCode(97 + file) + String(rank);
  }

  function uciToFromTo(uci) {
    const u = uci.toLowerCase().trim();
    if (u.length < 4) return null;
    return { from: u.slice(0, 2), to: u.slice(2, 4) };
  }

  /** Client-side FEN after one UCI (chess.js); null if unavailable or illegal. */
  function fenAfterUciClient(fen, uci) {
    if (typeof Chess === "undefined") return null;
    try {
      const g = new Chess(fen);
      const u = uci.toLowerCase();
      const from = u.slice(0, 2);
      const to = u.slice(2, 4);
      const promotion = u.length >= 5 ? u[4] : undefined;
      const m = g.move({ from: from, to: to, promotion: promotion });
      return m ? g.fen() : null;
    } catch (_) {
      return null;
    }
  }

  function fenAfterMoves(startFen, ucis) {
    let f = startFen;
    for (const u of ucis || []) {
      const next = fenAfterUciClient(f, u);
      if (!next) break;
      f = next;
    }
    return f;
  }

  function applyUciToGame(g, uci) {
    const u = uci.toLowerCase().trim();
    if (u.length < 4) return null;
    return g.move({
      from: u.slice(0, 2),
      to: u.slice(2, 4),
      promotion: u.length >= 5 ? u[4] : undefined,
    });
  }

  /** SAN including + / # when applicable; falls back to UCI if chess.js missing. */
  function uciToSanDisplay(uci, fenBefore) {
    if (typeof Chess === "undefined") return uci;
    try {
      const g = new Chess(fenBefore);
      const m = applyUciToGame(g, uci);
      return m ? m.san : uci;
    } catch (_) {
      return uci;
    }
  }

  function formatPvLineSans(lineUcis, startFen) {
    if (!lineUcis || !lineUcis.length) return "";
    if (typeof Chess === "undefined") return lineUcis.join(" · ");
    try {
      const g = new Chess(startFen);
      const parts = [];
      for (const uci of lineUcis) {
        const m = applyUciToGame(g, uci);
        parts.push(m ? m.san : uci);
      }
      return parts.join(" · ");
    } catch (_) {
      return lineUcis.join(" · ");
    }
  }

  function getAnalysisStrengthKey() {
    const s = el("analyze-strength");
    if (!s) return DEFAULT_STRENGTH;
    const v = s.value;
    return v in STRENGTH_SIMS || v === "custom" ? v : DEFAULT_STRENGTH;
  }

  function syncAnalyzeStrengthUI() {
    const sel = el("analyze-strength");
    const inp = el("analyze-sims");
    const cell = document.querySelector(".analyze-field--custom-sims");
    if (!sel || !inp) return;
    const key = sel.value;
    const isCustom = key === "custom";
    inp.disabled = !isCustom;
    if (!isCustom && STRENGTH_SIMS[key] != null) inp.value = String(STRENGTH_SIMS[key]);
    if (cell) cell.classList.toggle("analyze-field--custom-hidden", !isCustom);
  }

  function getAnalysisSims() {
    const key = getAnalysisStrengthKey();
    if (key === "custom") {
      const raw = parseInt(el("analyze-sims").value, 10);
      if (!Number.isFinite(raw)) return DEFAULT_ANALYSIS_SIMS;
      return Math.min(MAX_ANALYSIS_SIMS, Math.max(MIN_ANALYSIS_SIMS, raw));
    }
    return STRENGTH_SIMS[key] != null ? STRENGTH_SIMS[key] : DEFAULT_ANALYSIS_SIMS;
  }

  /** PV continuation strength scales with analysis strength. */
  function getPvFallbackSims() {
    const root = getAnalysisSims();
    return Math.max(12, Math.min(120, Math.round(root * 0.35)));
  }

  function getPvDepth() {
    const v = parseInt(el("pv-depth").value, 10);
    if (Number.isFinite(v) && v >= 2 && v <= MAX_PV_PLIES) return v;
    return 6;
  }

  function updatePvDeeperButton() {
    const btn = el("pv-deeper");
    if (!btn) return;
    const v = getPvDepth();
    btn.disabled = v >= MAX_PV_PLIES || !selectedCandidate;
  }

  function applyUciSequenceFromRoot(rootFen, ucis) {
    let f = rootFen;
    for (const u of ucis) {
      const next = fenAfterUciClient(f, u);
      if (!next) return null;
      f = next;
    }
    return f;
  }

  function setAnalyzePositionFromFen(fen) {
    if (!fen) return;
    const parts = fen.split(/\s+/);
    if (analyzeEditMode) {
      analyzeEditGrid = cloneGrid(parseFenBoard(fen));
      if (parts[1] === "w" || parts[1] === "b") el("analyze-turn").value = parts[1];
    }
    el("analyze-fen").value = fen;
    lastAnalyzeFen = fen;
    renderAnalyzeBoard();
  }

  /** Clickable SAN buttons; applies prefix from `rootFen` (analysis root), not the live field if edited. */
  function buildPvLineInteractive(rootFen, lineUcis) {
    const wrap = document.createElement("span");
    wrap.className = "pv-line-interactive";
    const ucis = lineUcis || [];
    if (!ucis.length) {
      wrap.textContent = "—";
      return wrap;
    }
    if (typeof Chess === "undefined") {
      wrap.textContent = ucis.join(" · ");
      return wrap;
    }
    try {
      const g = new Chess(rootFen);
      ucis.forEach((uci, i) => {
        const m = applyUciToGame(g, uci);
        const label = m ? m.san : uci;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pv-ply-btn";
        btn.textContent = label;
        btn.title = "Set board to position after " + uci + " (UCI)";
        btn.addEventListener("click", () => {
          const prefix = ucis.slice(0, i + 1);
          const endFen = applyUciSequenceFromRoot(rootFen, prefix);
          if (!endFen) {
            showToast("Could not apply that line from the saved analysis root.", 2000);
            return;
          }
          setAnalyzePositionFromFen(endFen);
          showToast("Board set to line position.", 1400);
        });
        wrap.appendChild(btn);
        if (i < ucis.length - 1) {
          const sep = document.createElement("span");
          sep.className = "pv-ply-sep";
          sep.textContent = "·";
          wrap.appendChild(sep);
        }
      });
    } catch (_) {
      wrap.textContent = formatPvLineSans(ucis, rootFen);
    }
    return wrap;
  }

  function renderPvResult(data, rootFenForPv) {
    const pvBox = el("analyze-pv");
    pvBox.innerHTML = "";

    const origin =
      data.pv_origin ||
      (data.mode === "greedy_research" ? "greedy" : data.mode === "tree_pv" ? "tree" : "");

    const lede = document.createElement("p");
    lede.className = "pv-lede";
    if (origin === "stockfish" || data.mode === "stockfish_pv") {
      lede.textContent = "Principal variation from Stockfish.";
    } else if (origin === "tree") {
      lede.textContent = "Variation along the search tree.";
    } else if (origin === "tree_plus_greedy") {
      lede.textContent = "Tree walk, then deeper search for the continuation.";
    } else if (origin === "greedy") {
      lede.textContent = "Variation built with fresh search at each step.";
    } else {
      lede.textContent = "Principal variation.";
    }
    pvBox.appendChild(lede);

    const details = document.createElement("details");
    details.className = "pv-tech-details";
    const sum = document.createElement("summary");
    sum.textContent = "How this line was built";
    details.appendChild(sum);
    const techBody = document.createElement("div");
    techBody.className = "pv-tech-body";
    let tech = "";
    if (origin === "stockfish" || data.mode === "stockfish_pv") {
      tech = "Stockfish principal variation at the configured depths.";
    } else if (origin === "tree") {
      tech = "Maximum-visit walk in the same Monte Carlo tree as Analyze (no greedy tail).";
    } else if (origin === "tree_plus_greedy") {
      tech = "Walk the stored tree while it expands, then new MCTS for deeper plies.";
    } else if (origin === "greedy") {
      tech = "When the move is missing from cache or tree, run a new search each step.";
    }
    techBody.textContent = tech;
    details.appendChild(techBody);
    if (data.evals_note) {
      const n = document.createElement("p");
      n.className = "pv-eval-note";
      n.textContent = String(data.evals_note);
      details.appendChild(n);
    }
    pvBox.appendChild(details);

    const lineRow = document.createElement("div");
    lineRow.className = "pv-line-block";
    const lineLabel = document.createElement("div");
    lineLabel.className = "pv-line-label";
    lineLabel.textContent = "Variation";
    lineRow.appendChild(lineLabel);
    lineRow.appendChild(buildPvLineInteractive(rootFenForPv, data.line || []));
    pvBox.appendChild(lineRow);

    const hint = document.createElement("p");
    hint.className = "pv-line-hint";
    hint.textContent = "Click a move to set the board to that position from your analyzed root.";
    pvBox.appendChild(hint);

    const evLabel =
      origin === "stockfish" || data.mode === "stockfish_pv"
        ? "Scores (side to move) along the line:"
        : data.mode === "tree_pv"
          ? "Q-values along the line:"
          : "Scores after each move:";
    const ev = (data.evals || []).map((v, i) => "m" + (i + 1) + "=" + v.toFixed(3)).join(", ");

    const evalsDiv = document.createElement("div");
    evalsDiv.className = "pv-evals";
    evalsDiv.textContent = evLabel + " " + (ev || "—");
    pvBox.appendChild(evalsDiv);

    updatePvDeeperButton();
  }

  function buildBoard(container, fen, opts) {
    const {
      onClick,
      onContextMenu,
      selected,
      legalTargets,
      highlightFrom,
      highlightTo,
      engineFlashUci,
    } = opts || {};
    const grid = parseFenBoard(fen);
    const flash = engineFlashUci ? uciToFromTo(engineFlashUci) : null;
    container.innerHTML = "";
    for (let row = 0; row < 8; row++) {
      for (let col = 0; col < 8; col++) {
        const sq = uciFromCoords(col, 8 - row);
        const cell = document.createElement("div");
        cell.className = "sq " + ((row + col) % 2 === 0 ? "light" : "dark");
        cell.dataset.sq = sq;
        const ch = grid[row][col];
        if (ch) {
          const img = document.createElement("img");
          img.src = "/pieces/" + PIECE_IMG[ch];
          img.alt = ch;
          if (flash && flash.to === sq) img.classList.add("piece-animate");
          cell.appendChild(img);
        }
        if (highlightFrom === sq) cell.classList.add("from-move");
        if (highlightTo === sq) cell.classList.add("to-move");
        if (flash && flash.to === sq) cell.classList.add("engine-move-flash");
        if (selected === sq) cell.classList.add("selected");
        if (legalTargets && legalTargets.has(sq)) cell.classList.add("legal-hint");
        if (onClick) cell.addEventListener("click", () => onClick(sq, ch));
        if (onContextMenu) {
          cell.addEventListener("contextmenu", (ev) => {
            ev.preventDefault();
            onContextMenu(sq, ch);
          });
        }
        container.appendChild(cell);
      }
    }
  }

  function showToast(message, ms) {
    const t = el("toast");
    t.textContent = message;
    t.classList.add("toast-visible");
    clearTimeout(showToast._tm);
    showToast._tm = setTimeout(() => t.classList.remove("toast-visible"), ms || 2200);
  }

  function legalTargetSet(fromSq, legalUcis) {
    const set = new Set();
    if (!fromSq || !legalUcis) return set;
    const prefix = fromSq.toLowerCase();
    for (const u of legalUcis) {
      const m = u.toLowerCase();
      if (m.length >= 4 && m.slice(0, 2) === prefix) {
        set.add(m.slice(2, 4));
      }
    }
    return set;
  }

  function findPromotionUci(fromSq, toSq, legalUcis) {
    const base = (fromSq + toSq).toLowerCase();
    for (const u of legalUcis) {
      const m = u.toLowerCase();
      if (m.startsWith(base)) return m;
    }
    return null;
  }

  function setStatus(which, text, isErr) {
    const n = el(which);
    n.textContent = text || "";
    n.classList.toggle("error", !!isErr);
  }

  function renderPlayBoard() {
    const fen = optimisticFenOverride || lastPlayFen || START_FEN;
    const legalTargets =
      playYourTurn && selectedSquare ? legalTargetSet(selectedSquare, playLegal) : new Set();
    buildBoard(el("play-board"), fen, {
      selected: selectedSquare,
      legalTargets,
      highlightFrom: playHighlightFrom,
      highlightTo: playHighlightTo,
      engineFlashUci: playEngineFlashUci,
      onClick: onPlaySquareClick,
    });
  }

  function flashPlayMove(uci, isEngine) {
    const ft = uciToFromTo(uci);
    if (!ft) return;
    playHighlightFrom = ft.from;
    playHighlightTo = ft.to;
    if (isEngine) playEngineFlashUci = uci;
    renderPlayBoard();
    clearTimeout(flashPlayMove._tm);
    flashPlayMove._tm = setTimeout(() => {
      playHighlightFrom = null;
      playHighlightTo = null;
      playEngineFlashUci = null;
      renderPlayBoard();
    }, 1200);
  }

  function onPlaySquareClick(sq, piece) {
    if (!playSessionId || !playYourTurn) return;

    if (selectedSquare === null) {
      if (!piece) return;
      selectedSquare = sq;
      renderPlayBoard();
      return;
    }

    if (sq === selectedSquare) {
      selectedSquare = null;
      renderPlayBoard();
      return;
    }

    let uci = (selectedSquare + sq).toLowerCase();
    if (!playLegal.some((m) => m.toLowerCase().startsWith(uci))) {
      if (piece) {
        selectedSquare = sq;
        renderPlayBoard();
      } else {
        selectedSquare = null;
        renderPlayBoard();
      }
      return;
    }

    const promo = findPromotionUci(selectedSquare, sq, playLegal);
    if (promo && promo.length === 5) uci = promo;
    else uci = (selectedSquare + sq).toLowerCase();

    selectedSquare = null;
    submitPlayMove(uci);
  }

  async function submitPlayMove(uci) {
    const uNorm = uci.toLowerCase();
    optimisticUci = uNorm;
    const previewFen = fenAfterUciClient(lastPlayFen || START_FEN, uci);
    optimisticFenOverride = previewFen;
    playYourTurn = false;
    playLegal = [];
    selectedSquare = null;
    flashPlayMove(uci, false);
    showToast("You played " + uciToSanDisplay(uNorm, lastPlayFen || START_FEN), 1600);
    renderMoveList(serverMoves.concat([uci]));
    renderPlayBoard();
    setStatus("play-status", "Engine thinking…");
    try {
      const res = await fetch("/api/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: playSessionId, uci }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      optimisticFenOverride = null;
      applyPlayState(data);
      setStatus("play-status", "");
    } catch (e) {
      optimisticUci = null;
      optimisticFenOverride = null;
      setStatus("play-status", String(e.message || e), true);
      try {
        const r = await fetch("/api/session/" + encodeURIComponent(playSessionId));
        const d = await r.json();
        if (r.ok) applyPlayState(d);
        else renderPlayBoard();
      } catch (_) {
        renderPlayBoard();
      }
      renderMoveList(serverMoves);
    }
  }

  function renderMoveList(moves) {
    const container = el("play-move-list");
    container.innerHTML = "";
    let walkFen = START_FEN;
    (moves || []).forEach((uci, i) => {
      const row = document.createElement("div");
      row.className = "move-row";
      const n = document.createElement("span");
      n.className = "ply-num";
      n.textContent = i + 1 + ".";
      const u = document.createElement("span");
      u.className = "ply-san";
      const san = uciToSanDisplay(uci, walkFen);
      u.textContent = san;
      u.title = uci;
      const next = fenAfterUciClient(walkFen, uci);
      if (next) walkFen = next;
      row.appendChild(n);
      row.appendChild(u);
      container.appendChild(row);
    });
    container.scrollTop = container.scrollHeight;
  }

  function clampPlayElo(n) {
    return Math.max(PLAY_ELO_MIN, Math.min(PLAY_ELO_MAX, n));
  }

  function setPlayEloBounds(lo, hi) {
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) return;
    PLAY_ELO_MIN = Math.round(lo);
    PLAY_ELO_MAX = Math.round(hi);
    const inp = el("play-engine-elo-input");
    const sld = el("play-engine-elo-slider");
    if (inp) {
      inp.min = String(PLAY_ELO_MIN);
      inp.max = String(PLAY_ELO_MAX);
    }
    if (sld) {
      sld.min = String(PLAY_ELO_MIN);
      sld.max = String(PLAY_ELO_MAX);
    }
    // Re-clamp current values so nothing pretends to be below the floor.
    playEloProgrammatic = true;
    try {
      if (inp) inp.value = String(clampPlayElo(parseInt(inp.value, 10) || PLAY_ELO_MIN));
      if (sld) sld.value = String(clampPlayElo(parseInt(sld.value, 10) || PLAY_ELO_MIN));
    } finally {
      playEloProgrammatic = false;
    }
  }

  function getPlayEngineElo() {
    if (!el("play-engine-use-elo") || !el("play-engine-use-elo").checked) return null;
    const raw = parseInt(el("play-engine-elo-input").value, 10);
    if (!Number.isFinite(raw)) return clampPlayElo(1600);
    return clampPlayElo(raw);
  }

  function syncPlayEloControlsDisabled() {
    const on = el("play-engine-use-elo") && el("play-engine-use-elo").checked;
    const wrap = el("play-elo-controls");
    const inp = el("play-engine-elo-input");
    const sld = el("play-engine-elo-slider");
    if (inp) inp.disabled = !on;
    if (sld) sld.disabled = !on;
    if (wrap) wrap.classList.toggle("play-elo-controls--off", !on);
  }

  function syncPlayEloSliderFromInput() {
    if (playEloProgrammatic) return;
    const inp = el("play-engine-elo-input");
    const sld = el("play-engine-elo-slider");
    if (!inp || !sld) return;
    const raw = parseInt(inp.value, 10);
    const v = clampPlayElo(Number.isFinite(raw) ? raw : 1600);
    inp.value = String(v);
    sld.value = String(v);
  }

  function syncPlayEloInputFromSlider() {
    if (playEloProgrammatic) return;
    const inp = el("play-engine-elo-input");
    const sld = el("play-engine-elo-slider");
    if (!inp || !sld) return;
    const v = clampPlayElo(parseInt(sld.value, 10));
    inp.value = String(v);
  }

  function setPlayEngineControlsFromServer(engineElo) {
    playEloProgrammatic = true;
    const chk = el("play-engine-use-elo");
    const inp = el("play-engine-elo-input");
    const sld = el("play-engine-elo-slider");
    try {
      if (!chk || !inp || !sld) return;
      if (engineElo == null || engineElo === "") {
        chk.checked = false;
        inp.value = "1600";
        sld.value = "1600";
      } else {
        chk.checked = true;
        const v = clampPlayElo(engineElo);
        inp.value = String(v);
        sld.value = String(v);
      }
      syncPlayEloControlsDisabled();
    } finally {
      setTimeout(function () {
        playEloProgrammatic = false;
      }, 0);
    }
  }

  async function patchPlayEngineElo() {
    if (!playSessionId) return;
    try {
      const res = await fetch("/api/session/" + encodeURIComponent(playSessionId), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine_elo: getPlayEngineElo() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      applyPlayState(data);
    } catch (e) {
      showToast(String(e.message || e), 3200);
    }
  }

  function updatePlayPgnUI() {
    const copyBtn = el("btn-copy-play-pgn");
    const revBtn = el("btn-play-to-review");
    const dl = el("play-pgn-download");
    const ok = !!playSessionId;
    if (copyBtn) copyBtn.disabled = !ok;
    if (revBtn) revBtn.disabled = !ok;
    if (dl) {
      if (ok) {
        dl.setAttribute("href", "/api/session/" + encodeURIComponent(playSessionId) + "/pgn");
        dl.classList.remove("play-pgn-download--disabled");
        dl.removeAttribute("aria-disabled");
      } else {
        dl.setAttribute("href", "#");
        dl.classList.add("play-pgn-download--disabled");
        dl.setAttribute("aria-disabled", "true");
      }
    }
  }

  async function copyPlayPgnToClipboard() {
    const text = lastPlayPgn || "";
    if (!text.trim()) {
      showToast("No PGN to copy yet.", 2000);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast("PGN copied to clipboard.", 1800);
    } catch (e) {
      showToast(String(e.message || e), 3200);
    }
  }

  function openPlayPgnInReview() {
    const ta = el("review-pgn");
    if (ta) ta.value = lastPlayPgn || "";
    const tab = el("tab-review");
    if (tab) tab.click();
    if (ta) {
      ta.focus();
      ta.scrollIntoView({ block: "nearest" });
    }
  }

  function applyPlayState(data) {
    const moves = data.moves || [];
    const priorLen = prevMoveLen;
    const added = moves.slice(priorLen);
    prevMoveLen = moves.length;
    serverMoves = moves;

    const humanJustPlayed = optimisticUci;
    let walkFen = fenAfterMoves(START_FEN, moves.slice(0, priorLen));
    for (const uci of added) {
      const u = uci.toLowerCase();
      if (humanJustPlayed && u === humanJustPlayed) {
        walkFen = fenAfterUciClient(walkFen, uci) || walkFen;
        continue;
      }
      flashPlayMove(uci, true);
      showToast("Engine played " + uciToSanDisplay(uci, walkFen), 2400);
      walkFen = fenAfterUciClient(walkFen, uci) || walkFen;
    }
    optimisticUci = null;

    lastPlayFen = data.fen || lastPlayFen;
    optimisticFenOverride = null;
    playYourTurn = !!data.your_turn;
    playLegal = data.legal_ucis || [];
    if (!playYourTurn) selectedSquare = null;
    setPlayEngineControlsFromServer(data.engine_elo);
    const eloNote =
      data.engine_elo != null
        ? "Opponent ~" + data.engine_elo + " Elo (play). "
        : "Opponent full strength (play). ";
    el("play-side-info").textContent =
      eloNote +
      (data.terminal
        ? "Game over: " + (data.result || "")
        : data.your_turn
          ? "Your turn (" + (data.side_to_move || "") + " to move)."
          : "Engine to move…");
    renderMoveList(moves);
    lastPlayPgn = typeof data.pgn === "string" ? data.pgn : "";
    updatePlayPgnUI();
    renderPlayBoard();
  }

  async function newGame() {
    setStatus("play-status", "Starting…");
    humanColorChoice = el("play-color").value;
    prevMoveLen = 0;
    serverMoves = [];
    optimisticUci = null;
    optimisticFenOverride = null;
    playHighlightFrom = null;
    playHighlightTo = null;
    playEngineFlashUci = null;
    try {
      const res = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ human: humanColorChoice, engine_elo: getPlayEngineElo() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      playSessionId = data.session_id;
      selectedSquare = null;
      applyPlayState(data);
      setStatus("play-status", "");
    } catch (e) {
      setStatus("play-status", String(e.message || e), true);
    }
  }

  function getAnalyzeFenFromForm() {
    const fen = el("analyze-fen").value.trim();
    const turn = el("analyze-turn").value;
    if (analyzeEditMode && analyzeEditGrid) {
      return buildFenFromGrid(analyzeEditGrid, turn, readEditorCastling());
    }
    if (!fen) return START_FEN;
    const parts = fen.split(/\s+/);
    if (parts.length >= 2) return fen;
    return `${parts[0]} ${turn} - - 0 1`;
  }

  function syncAnalyzeFenField() {
    const f = getAnalyzeFenFromForm();
    el("analyze-fen").value = f;
    lastAnalyzeFen = f;
    if (analyzeEditMode) validateEditorFen();
  }

  function renderAnalyzeBoard() {
    const turn = el("analyze-turn").value;
    let fen;
    if (analyzeEditMode && analyzeEditGrid) {
      fen = buildFenFromGrid(analyzeEditGrid, turn, readEditorCastling());
    } else {
      fen = el("analyze-fen").value.trim() || lastAnalyzeFen || START_FEN;
    }
    buildBoard(el("analyze-board"), fen, {
      onClick: analyzeEditMode ? onAnalyzeSquareClick : null,
      onContextMenu: analyzeEditMode ? onAnalyzeContextMenu : null,
    });
    if (analyzeEditMode) {
      attachEditorBoardHandlers();
      updateBoardCursorMode();
    }
  }

  function sqToRC(sq) {
    return [8 - parseInt(sq[1], 10), sq.charCodeAt(0) - 97];
  }

  function clearPaletteSelection() {
    document.querySelectorAll(".palette-piece").forEach((b) => b.classList.remove("palette-selected"));
    const er = el("editor-eraser-btn");
    if (er) er.setAttribute("aria-pressed", "false");
    const mv = el("editor-move-btn");
    if (mv) mv.setAttribute("aria-pressed", "false");
  }

  function updateEditorSelectedToolUI() {
    const wrap = el("editor-selected-tool");
    const iconSlot = el("editor-selected-tool-icon");
    const labelSlot = el("editor-selected-tool-label");
    if (!wrap || !iconSlot || !labelSlot) return;
    wrap.classList.remove(
      "editor-selected-tool--active",
      "editor-selected-tool--eraser",
      "editor-selected-tool--move",
      "editor-selected-tool--empty"
    );
    iconSlot.innerHTML = "";
    iconSlot.classList.remove(
      "editor-selected-tool-icon--eraser",
      "editor-selected-tool-icon--move"
    );
    if (analyzeHand && analyzeHand.eraser) {
      wrap.classList.add("editor-selected-tool--eraser");
      iconSlot.classList.add("editor-selected-tool-icon--eraser");
      iconSlot.textContent = "⌫";
      labelSlot.textContent = "Eraser";
    } else if (analyzeHand && analyzeHand.char) {
      wrap.classList.add("editor-selected-tool--active");
      const img = document.createElement("img");
      img.src = "/pieces/" + PIECE_IMG[analyzeHand.char];
      img.alt = analyzeHand.char;
      iconSlot.appendChild(img);
      labelSlot.textContent = TOOL_LABELS[analyzeHand.char] || analyzeHand.char;
    } else {
      wrap.classList.add("editor-selected-tool--move");
      iconSlot.classList.add("editor-selected-tool-icon--move");
      iconSlot.textContent = "✥";
      labelSlot.textContent = "Move pieces";
    }
  }

  function updateEditorHelper() {
    const line = el("analyze-editor-helper");
    if (!line) return;
    if (analyzeHand && analyzeHand.eraser) {
      line.textContent = "Click a square to remove its piece. Right-click also clears.";
    } else if (analyzeHand && analyzeHand.char) {
      const name = (TOOL_LABELS[analyzeHand.char] || "piece").toLowerCase();
      line.textContent = "Click a square to place a " + name + ". Right-click clears.";
    } else {
      line.textContent = "Drag pieces to move them, or select a piece to place.";
    }
  }

  function updateBoardCursorMode() {
    const board = el("analyze-board");
    if (!board) return;
    if (!analyzeEditMode) {
      board.removeAttribute("data-editor-tool");
      return;
    }
    let mode = "none";
    if (analyzeHand && analyzeHand.eraser) mode = "eraser";
    else if (analyzeHand && analyzeHand.char) mode = "piece";
    board.setAttribute("data-editor-tool", mode);
  }

  function setEditorTool(tool) {
    analyzeHand = tool;
    clearPaletteSelection();
    if (tool && tool.char) {
      const btn = document.querySelector('.palette-piece[data-piece="' + tool.char + '"]');
      if (btn) btn.classList.add("palette-selected");
    } else if (tool && tool.eraser) {
      const er = el("editor-eraser-btn");
      if (er) er.setAttribute("aria-pressed", "true");
    } else {
      const mv = el("editor-move-btn");
      if (mv) mv.setAttribute("aria-pressed", "true");
    }
    updateEditorSelectedToolUI();
    updateEditorHelper();
    updateBoardCursorMode();
    clearEditorHoverPreview();
  }

  /* —— Undo stack: snapshot before any grid-changing action. —— */
  function snapshotEditorState() {
    return {
      grid: cloneGrid(analyzeEditGrid),
      turn: el("analyze-turn").value,
      castling: readEditorCastling(),
    };
  }

  function pushUndo() {
    if (!analyzeEditMode || !analyzeEditGrid) return;
    editorUndoStack.push(snapshotEditorState());
    if (editorUndoStack.length > EDITOR_UNDO_MAX) editorUndoStack.shift();
    updateUndoButton();
  }

  function performUndo() {
    if (!analyzeEditMode || !editorUndoStack.length) return;
    const s = editorUndoStack.pop();
    analyzeEditGrid = s.grid;
    el("analyze-turn").value = s.turn;
    syncEditorSideUI(s.turn);
    syncEditorCastlingUI(s.castling);
    updateUndoButton();
    syncAnalyzeFenField();
    renderAnalyzeBoard();
  }

  function updateUndoButton() {
    const btn = el("editor-undo");
    if (btn) btn.disabled = editorUndoStack.length === 0;
  }

  function resetEditorUndo() {
    editorUndoStack = [];
    updateUndoButton();
  }

  function readEditorCastling() {
    if (!analyzeEditMode) return parseCastlingFromFen(lastAnalyzeFen);
    let s = "";
    CASTLE_KEYS.forEach((k) => {
      const c = el("editor-castle-" + k);
      if (c && c.checked) s += k;
    });
    return s || "-";
  }

  function syncEditorCastlingUI(castlingStr) {
    const str = castlingStr || "-";
    CASTLE_KEYS.forEach((k) => {
      const c = el("editor-castle-" + k);
      if (c) c.checked = str !== "-" && str.includes(k);
    });
  }

  function syncEditorSideUI(turn) {
    document.querySelectorAll("#editor-side-segmented .editor-seg").forEach((b) => {
      b.classList.toggle("editor-seg--on", b.dataset.side === turn);
    });
  }

  /**
   * True validator for editor positions. Covers everything a real chess engine
   * cares about before it will accept a position:
   *  - exactly one king per side, not adjacent
   *  - at most 8 pawns per side, none on the 1st/8th rank
   *  - at most 16 pieces per side
   *  - promoted-piece arithmetic: extra officers must be paid for by missing pawns
   *  - side-not-to-move cannot be in check (and both kings in check is impossible)
   *  - castling rights require the king and matching rook on their home squares
   *  - side-to-move must be "w" or "b"
   * Returns { ok: bool, message: string }.
   */
  function validateEditorPosition(fen) {
    const parts = (fen || "").trim().split(/\s+/);
    if (parts.length < 4) return { ok: false, message: "Malformed FEN." };
    const [placement, side, castling] = parts;

    const ranks = placement.split("/");
    if (ranks.length !== 8) return { ok: false, message: "Board must have 8 ranks." };
    const grid = [];
    for (let r = 0; r < 8; r++) {
      const row = [];
      for (const ch of ranks[r]) {
        if (ch >= "1" && ch <= "8") {
          const n = ch.charCodeAt(0) - 48;
          for (let i = 0; i < n; i++) row.push(null);
        } else if ("prnbqkPRNBQK".indexOf(ch) !== -1) {
          row.push(ch);
        } else {
          return { ok: false, message: "Invalid board character: '" + ch + "'." };
        }
      }
      if (row.length !== 8) {
        return { ok: false, message: "Rank " + (8 - r) + " has " + row.length + " squares (need 8)." };
      }
      grid.push(row);
    }

    if (side !== "w" && side !== "b") {
      return { ok: false, message: "Side to move must be White or Black." };
    }

    const counts = { K: 0, Q: 0, R: 0, B: 0, N: 0, P: 0, k: 0, q: 0, r: 0, b: 0, n: 0, p: 0 };
    let wkPos = null;
    let bkPos = null;
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 8; c++) {
        const p = grid[r][c];
        if (!p) continue;
        counts[p]++;
        if (p === "K") wkPos = { r, c };
        else if (p === "k") bkPos = { r, c };
      }
    }

    if (counts.K === 0) return { ok: false, message: "White has no king." };
    if (counts.K > 1) return { ok: false, message: "White has more than one king." };
    if (counts.k === 0) return { ok: false, message: "Black has no king." };
    if (counts.k > 1) return { ok: false, message: "Black has more than one king." };

    if (Math.max(Math.abs(wkPos.r - bkPos.r), Math.abs(wkPos.c - bkPos.c)) <= 1) {
      return { ok: false, message: "Kings cannot stand on adjacent squares." };
    }

    for (let c = 0; c < 8; c++) {
      if (grid[0][c] === "P" || grid[0][c] === "p") {
        return { ok: false, message: "A pawn cannot stand on the 8th rank." };
      }
      if (grid[7][c] === "P" || grid[7][c] === "p") {
        return { ok: false, message: "A pawn cannot stand on the 1st rank." };
      }
    }

    if (counts.P > 8) return { ok: false, message: "White has more than 8 pawns." };
    if (counts.p > 8) return { ok: false, message: "Black has more than 8 pawns." };

    const whiteTotal = counts.K + counts.Q + counts.R + counts.B + counts.N + counts.P;
    const blackTotal = counts.k + counts.q + counts.r + counts.b + counts.n + counts.p;
    if (whiteTotal > 16) return { ok: false, message: "White has more than 16 pieces." };
    if (blackTotal > 16) return { ok: false, message: "Black has more than 16 pieces." };

    /* Each extra officer beyond the starting count costs one pawn (promoted),
       so pawns + extra_officers must not exceed 8 per side. */
    const promoW =
      Math.max(0, counts.Q - 1) +
      Math.max(0, counts.R - 2) +
      Math.max(0, counts.B - 2) +
      Math.max(0, counts.N - 2);
    const promoB =
      Math.max(0, counts.q - 1) +
      Math.max(0, counts.r - 2) +
      Math.max(0, counts.b - 2) +
      Math.max(0, counts.n - 2);
    if (counts.P + promoW > 8) {
      return { ok: false, message: "Too many white promoted pieces for the pawn count." };
    }
    if (counts.p + promoB > 8) {
      return { ok: false, message: "Too many black promoted pieces for the pawn count." };
    }

    if (castling && castling !== "-") {
      if (!/^[KQkq]+$/.test(castling)) {
        return { ok: false, message: "Castling rights must use only K, Q, k, q." };
      }
      const seen = new Set();
      for (const ch of castling) {
        if (seen.has(ch)) return { ok: false, message: "Duplicate castling right: " + ch + "." };
        seen.add(ch);
      }
      if (castling.indexOf("K") !== -1) {
        if (!(wkPos.r === 7 && wkPos.c === 4)) return { ok: false, message: "Castling K requires the white king on e1." };
        if (grid[7][7] !== "R") return { ok: false, message: "Castling K requires a white rook on h1." };
      }
      if (castling.indexOf("Q") !== -1) {
        if (!(wkPos.r === 7 && wkPos.c === 4)) return { ok: false, message: "Castling Q requires the white king on e1." };
        if (grid[7][0] !== "R") return { ok: false, message: "Castling Q requires a white rook on a1." };
      }
      if (castling.indexOf("k") !== -1) {
        if (!(bkPos.r === 0 && bkPos.c === 4)) return { ok: false, message: "Castling k requires the black king on e8." };
        if (grid[0][7] !== "r") return { ok: false, message: "Castling k requires a black rook on h8." };
      }
      if (castling.indexOf("q") !== -1) {
        if (!(bkPos.r === 0 && bkPos.c === 4)) return { ok: false, message: "Castling q requires the black king on e8." };
        if (grid[0][0] !== "r") return { ok: false, message: "Castling q requires a black rook on a8." };
      }
    }

    const whiteInCheck = isSquareAttacked(grid, wkPos.r, wkPos.c, "black");
    const blackInCheck = isSquareAttacked(grid, bkPos.r, bkPos.c, "white");
    if (whiteInCheck && blackInCheck) {
      return { ok: false, message: "Both kings cannot be in check at the same time." };
    }
    if (side === "w" && blackInCheck) {
      return { ok: false, message: "Black is in check but it is White to move." };
    }
    if (side === "b" && whiteInCheck) {
      return { ok: false, message: "White is in check but it is Black to move." };
    }

    return { ok: true, message: "Valid position." };
  }

  /** True iff (r, c) is attacked by any piece of `byColor` ("white" | "black"). */
  function isSquareAttacked(grid, r, c, byColor) {
    const isWhite = byColor === "white";
    const pawn = isWhite ? "P" : "p";
    const knight = isWhite ? "N" : "n";
    const bishop = isWhite ? "B" : "b";
    const rook = isWhite ? "R" : "r";
    const queen = isWhite ? "Q" : "q";
    const king = isWhite ? "K" : "k";

    /* Pawn attacks: a white pawn on (pr, pc) attacks (pr-1, pc±1), so the
       attacker sits one row below (toward rank 1) in our row-from-top grid. */
    const pr = isWhite ? r + 1 : r - 1;
    if (pr >= 0 && pr < 8) {
      if (c - 1 >= 0 && grid[pr][c - 1] === pawn) return true;
      if (c + 1 < 8 && grid[pr][c + 1] === pawn) return true;
    }

    const knightOffsets = [
      [-2, -1], [-2, 1], [-1, -2], [-1, 2],
      [1, -2], [1, 2], [2, -1], [2, 1],
    ];
    for (let i = 0; i < knightOffsets.length; i++) {
      const nr = r + knightOffsets[i][0];
      const nc = c + knightOffsets[i][1];
      if (nr >= 0 && nr < 8 && nc >= 0 && nc < 8 && grid[nr][nc] === knight) return true;
    }

    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) {
        if (dr === 0 && dc === 0) continue;
        const nr = r + dr;
        const nc = c + dc;
        if (nr >= 0 && nr < 8 && nc >= 0 && nc < 8 && grid[nr][nc] === king) return true;
      }
    }

    const orthoDirs = [[-1, 0], [1, 0], [0, -1], [0, 1]];
    for (let i = 0; i < orthoDirs.length; i++) {
      let nr = r + orthoDirs[i][0];
      let nc = c + orthoDirs[i][1];
      while (nr >= 0 && nr < 8 && nc >= 0 && nc < 8) {
        const p = grid[nr][nc];
        if (p) {
          if (p === rook || p === queen) return true;
          break;
        }
        nr += orthoDirs[i][0];
        nc += orthoDirs[i][1];
      }
    }

    const diagDirs = [[-1, -1], [-1, 1], [1, -1], [1, 1]];
    for (let i = 0; i < diagDirs.length; i++) {
      let nr = r + diagDirs[i][0];
      let nc = c + diagDirs[i][1];
      while (nr >= 0 && nr < 8 && nc >= 0 && nc < 8) {
        const p = grid[nr][nc];
        if (p) {
          if (p === bishop || p === queen) return true;
          break;
        }
        nr += diagDirs[i][0];
        nc += diagDirs[i][1];
      }
    }

    return false;
  }

  function validateEditorFen() {
    const box = el("editor-validation");
    const doneBtn = el("editor-done");
    if (!box) return;
    const fen = getAnalyzeFenFromForm();
    box.classList.remove(
      "editor-validation--ok",
      "editor-validation--warn",
      "editor-validation--error"
    );
    const result = validateEditorPosition(fen);
    if (result.ok) {
      box.classList.add("editor-validation--ok");
      box.textContent = "Valid position.";
    } else {
      box.classList.add("editor-validation--error");
      box.textContent = result.message;
    }
    if (doneBtn) doneBtn.disabled = !result.ok;
  }

  function setEditorGridFromFen(fen) {
    analyzeEditGrid = cloneGrid(parseFenBoard(fen));
    const parts = fen.split(/\s+/);
    const turn = parts[1] === "b" ? "b" : "w";
    el("analyze-turn").value = turn;
    syncEditorSideUI(turn);
    syncEditorCastlingUI(parseCastlingFromFen(fen));
  }

  function enterAnalyzeEditMode() {
    const base = el("analyze-fen").value.trim() || lastAnalyzeFen || START_FEN;
    editorSnapshot = {
      fen: base,
      turn: (base.split(/\s+/)[1] === "b" ? "b" : "w"),
      castling: parseCastlingFromFen(base),
    };
    analyzeEditMode = true;
    analyzeHand = null;
    editorDragSrc = null;
    resetEditorUndo();
    setEditorGridFromFen(base);
    const layout = el("analyze-layout");
    if (layout) layout.classList.add("layout--analyze--editing");
    setEditorTool(null);
    syncAnalyzeFenField();
    renderAnalyzeBoard();
    validateEditorFen();
  }

  function exitAnalyzeEditMode(commit) {
    if (!commit && editorSnapshot) {
      el("analyze-fen").value = editorSnapshot.fen;
      lastAnalyzeFen = editorSnapshot.fen;
      el("analyze-turn").value = editorSnapshot.turn;
    } else {
      syncAnalyzeFenField();
    }
    analyzeEditMode = false;
    analyzeEditGrid = null;
    analyzeHand = null;
    editorSnapshot = null;
    editorDragSrc = null;
    resetEditorUndo();
    const layout = el("analyze-layout");
    if (layout) layout.classList.remove("layout--analyze--editing");
    clearPaletteSelection();
    updateBoardCursorMode();
    renderAnalyzeBoard();
  }

  /**
   * Click model (sticky tool):
   * - Neutral (analyzeHand == null): clicks do nothing — drag handles moves.
   * - Piece tool: click places that piece (replacing anything on the square).
   * - Eraser: click removes the square's piece.
   */
  function onAnalyzeSquareClick(sq, piece) {
    if (!analyzeEditMode || !analyzeEditGrid) return;
    const [r, c] = sqToRC(sq);

    if (analyzeHand && analyzeHand.eraser) {
      if (analyzeEditGrid[r][c] == null) return;
      pushUndo();
      analyzeEditGrid[r][c] = null;
      syncAnalyzeFenField();
      renderAnalyzeBoard();
      return;
    }

    if (analyzeHand && analyzeHand.char) {
      if (analyzeEditGrid[r][c] === analyzeHand.char) return;
      pushUndo();
      analyzeEditGrid[r][c] = analyzeHand.char;
      syncAnalyzeFenField();
      renderAnalyzeBoard();
      return;
    }
    /* Neutral: left-click is intentionally a no-op. Use drag to move. */
  }

  /* Right-click always clears the square, regardless of the selected tool. */
  function onAnalyzeContextMenu(sq, piece) {
    if (!analyzeEditMode || !analyzeEditGrid) return;
    const [r, c] = sqToRC(sq);
    if (analyzeEditGrid[r][c] == null) return;
    pushUndo();
    analyzeEditGrid[r][c] = null;
    syncAnalyzeFenField();
    renderAnalyzeBoard();
  }

  function buildPalette() {
    const wrap = el("editor-pieces");
    if (!wrap) return;
    wrap.innerHTML = "";
    const addPiece = (ch) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "palette-piece";
      b.dataset.piece = ch;
      const img = document.createElement("img");
      img.src = "/pieces/" + PIECE_IMG[ch];
      img.alt = ch;
      b.appendChild(img);
      b.addEventListener("click", () => {
        if (analyzeHand && analyzeHand.char === ch) {
          setEditorTool(null);
        } else {
          setEditorTool({ char: ch });
        }
      });
      wrap.appendChild(b);
    };
    PALETTE_WHITE.forEach(addPiece);
    PALETTE_BLACK.forEach(addPiece);
  }

  /* —— Board-side editor handlers: hover preview + drag-to-move. Attached
     fresh after each board render; classes/listeners live on cell DOM. —— */

  function clearEditorHoverPreview() {
    document.querySelectorAll("#analyze-board .sq.sq--place-preview, #analyze-board .sq.sq--erase-preview").forEach((c) => {
      c.classList.remove("sq--place-preview", "sq--erase-preview");
    });
    document.querySelectorAll("#analyze-board .piece-preview").forEach((p) => p.remove());
  }

  function clearEditorDragVisuals() {
    document.querySelectorAll("#analyze-board .sq--drag-source, #analyze-board .sq--drag-target").forEach((c) => {
      c.classList.remove("sq--drag-source", "sq--drag-target");
    });
  }

  function onEditorSquareEnter(sq, cell) {
    if (!analyzeEditMode || editorDragSrc) return;
    if (analyzeHand && analyzeHand.char) {
      cell.classList.add("sq--place-preview");
      const existing = cell.querySelector(".piece-preview");
      if (existing) existing.remove();
      const ghost = document.createElement("img");
      ghost.className = "piece-preview";
      ghost.src = "/pieces/" + PIECE_IMG[analyzeHand.char];
      ghost.alt = "";
      ghost.draggable = false;
      ghost.setAttribute("aria-hidden", "true");
      cell.appendChild(ghost);
    } else if (analyzeHand && analyzeHand.eraser) {
      const [r, c] = sqToRC(sq);
      if (analyzeEditGrid && analyzeEditGrid[r][c] != null) {
        cell.classList.add("sq--erase-preview");
      }
    }
  }

  function onEditorSquareLeave(sq, cell) {
    cell.classList.remove("sq--place-preview", "sq--erase-preview");
    const prev = cell.querySelector(".piece-preview");
    if (prev) prev.remove();
  }

  function onEditorDragStart(ev, sq, cell) {
    if (!analyzeEditMode || !analyzeEditGrid) { ev.preventDefault(); return; }
    if (analyzeHand) { ev.preventDefault(); return; }
    const [r, c] = sqToRC(sq);
    const piece = analyzeEditGrid[r][c];
    if (!piece) { ev.preventDefault(); return; }
    editorDragSrc = { sq, piece };
    cell.classList.add("sq--drag-source");
    clearEditorHoverPreview();
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
      try { ev.dataTransfer.setData("text/plain", sq); } catch (e) { /* ignore */ }
      const img = cell.querySelector("img:not(.piece-preview)");
      if (img && ev.dataTransfer.setDragImage) {
        const rect = img.getBoundingClientRect();
        ev.dataTransfer.setDragImage(img, rect.width / 2, rect.height / 2);
      }
    }
  }

  function onEditorDragOver(ev, sq, cell) {
    if (!editorDragSrc) return;
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
    if (!cell.classList.contains("sq--drag-target")) {
      document.querySelectorAll("#analyze-board .sq--drag-target").forEach((c) => c.classList.remove("sq--drag-target"));
      cell.classList.add("sq--drag-target");
    }
  }

  function onEditorDrop(ev, sq, cell) {
    ev.preventDefault();
    const src = editorDragSrc;
    editorDragSrc = null;
    clearEditorDragVisuals();
    if (!src) return;
    if (src.sq === sq) return;
    const [sr, sc] = sqToRC(src.sq);
    const [tr, tc] = sqToRC(sq);
    const piece = analyzeEditGrid[sr][sc];
    if (!piece) return;
    pushUndo();
    analyzeEditGrid[tr][tc] = piece;
    analyzeEditGrid[sr][sc] = null;
    syncAnalyzeFenField();
    renderAnalyzeBoard();
  }

  function onEditorDragEnd() {
    editorDragSrc = null;
    clearEditorDragVisuals();
  }

  function attachEditorBoardHandlers() {
    const board = el("analyze-board");
    if (!board) return;
    board.querySelectorAll(".sq").forEach((cell) => {
      const sq = cell.dataset.sq;
      const hasPiece = !!cell.querySelector("img");
      /* Hover preview (suppressed during drag). */
      cell.addEventListener("mouseenter", () => onEditorSquareEnter(sq, cell));
      cell.addEventListener("mouseleave", () => onEditorSquareLeave(sq, cell));
      /* Drag-to-move only activates when the cell holds a piece. */
      if (hasPiece) cell.draggable = true;
      cell.addEventListener("dragstart", (ev) => onEditorDragStart(ev, sq, cell));
      cell.addEventListener("dragover", (ev) => onEditorDragOver(ev, sq, cell));
      cell.addEventListener("dragleave", () => cell.classList.remove("sq--drag-target"));
      cell.addEventListener("drop", (ev) => onEditorDrop(ev, sq, cell));
      cell.addEventListener("dragend", onEditorDragEnd);
    });
  }

  function setupCandidateCollapse(rows) {
    const moreRow = el("analyze-moves-more");
    if (!moreRow) return;
    moreRow.innerHTML = "";
    if (!rows || rows.length <= CANDIDATES_VISIBLE_DEFAULT) {
      moreRow.classList.add("hidden");
      return;
    }
    const hiddenCount = rows.length - CANDIDATES_VISIBLE_DEFAULT;
    rows.forEach((r, i) => {
      if (i >= CANDIDATES_VISIBLE_DEFAULT) r.classList.add("hidden");
    });
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "candidate-more-btn";
    btn.textContent = "Show " + hiddenCount + " more";
    btn.addEventListener("click", () => {
      const collapsed = rows.some((r, i) => i >= CANDIDATES_VISIBLE_DEFAULT && r.classList.contains("hidden"));
      rows.forEach((r, i) => {
        if (i >= CANDIDATES_VISIBLE_DEFAULT) r.classList.toggle("hidden", !collapsed);
      });
      btn.textContent = collapsed ? "Show fewer" : "Show " + hiddenCount + " more";
    });
    moreRow.appendChild(btn);
    moreRow.classList.remove("hidden");
  }

  async function runAnalyze() {
    if (analyzeEditMode) syncAnalyzeFenField();
    const fen = el("analyze-fen").value.trim();
    if (!fen) {
      setStatus("analyze-status", "Enter a FEN or use the editor.", true);
      return;
    }
    if (analyzeFetchController) analyzeFetchController.abort();
    analyzeFetchController = new AbortController();
    const { signal } = analyzeFetchController;
    setStatus("analyze-status", "Analyzing…");
    el("analyze-moves").innerHTML = "";
    const moreRow = el("analyze-moves-more");
    if (moreRow) {
      moreRow.innerHTML = "";
      moreRow.classList.add("hidden");
    }
    el("analyze-pv").innerHTML = "";
    const explainBox = el("analyze-explain");
    if (explainBox) explainBox.innerHTML = "";
    el("analyze-root-eval").textContent = "";
    selectedCandidate = null;
    updatePvDeeperButton();
    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fen,
          sims: getAnalysisSims(),
          strength: getAnalysisStrengthKey(),
          top_k: ANALYSIS_TOP_K,
        }),
        signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      lastAnalyzeFen = data.fen || fen;
      el("analyze-fen").value = lastAnalyzeFen;
      if (analyzeEditMode) {
        analyzeEditGrid = cloneGrid(parseFenBoard(lastAnalyzeFen));
        const parts = lastAnalyzeFen.split(/\s+/);
        if (parts[1]) el("analyze-turn").value = parts[1];
      }
      renderAnalyzeBoard();
      if (data.terminal) {
        setStatus("analyze-status", "Terminal: " + (data.result || ""));
        updatePvDeeperButton();
        return;
      }
      const rv = data.root_value;
      const meta = data.analysis || {};
      const metaBits = [];
      if (meta.depth != null) metaBits.push("depth " + meta.depth);
      if (meta.seldepth != null) metaBits.push("seldepth " + meta.seldepth);
      if (meta.time != null) metaBits.push((meta.time / 1000).toFixed(2) + "s");
      const metaStr = metaBits.length ? " · " + metaBits.join(", ") : "";
      const bestLabel =
        data.root_score_label != null
          ? data.root_score_label
          : rv != null
            ? "q=" + rv.toFixed(3)
            : "—";
      el("analyze-root-eval").textContent = "Best: " + bestLabel + metaStr;
      const ul = el("analyze-moves");
      const rootFen = data.fen || fen;
      const rows = (data.moves || []).map((m, idx) => {
        const li = document.createElement("li");
        li.className = "candidate-move";
        if (idx === 0) li.classList.add("candidate-move--best");

        const sanEl = document.createElement("span");
        sanEl.className = "candidate-move__san";
        sanEl.textContent = uciToSanDisplay(m.uci, rootFen);
        if (m.mate_in_one) {
          const mateBadge = document.createElement("span");
          mateBadge.className = "candidate-move__mate";
          mateBadge.textContent = "#1";
          mateBadge.title = "Mate in one";
          sanEl.appendChild(mateBadge);
        }

        const evalEl = document.createElement("span");
        evalEl.className = "candidate-move__eval";
        evalEl.textContent = m.score_label != null ? m.score_label : "q=" + m.q.toFixed(3);

        const shareEl = document.createElement("span");
        shareEl.className = "candidate-move__share";
        const pctNum = Number.isFinite(m.visit_pct) ? m.visit_pct : 0;
        const pctEl = document.createElement("span");
        pctEl.className = "candidate-move__pct";
        pctEl.textContent = pctNum.toFixed(0) + "%";
        const barEl = document.createElement("span");
        barEl.className = "candidate-move__bar";
        barEl.setAttribute("aria-hidden", "true");
        const barFill = document.createElement("span");
        barFill.className = "candidate-move__bar-fill";
        barFill.style.width = Math.max(0, Math.min(100, pctNum)).toFixed(1) + "%";
        barEl.appendChild(barFill);
        shareEl.appendChild(pctEl);
        shareEl.appendChild(barEl);

        li.appendChild(sanEl);
        li.appendChild(evalEl);
        li.appendChild(shareEl);
        li.title = "UCI " + m.uci + " · visit share " + pctNum.toFixed(1) + "%";
        li.dataset.uci = m.uci;
        li.addEventListener("click", () => onCandidateClick(m.uci, li));
        ul.appendChild(li);
        return li;
      });
      setupCandidateCollapse(rows);
      setStatus("analyze-status", "");
      updatePvDeeperButton();
    } catch (e) {
      if (e.name === "AbortError") return;
      setStatus("analyze-status", String(e.message || e), true);
      updatePvDeeperButton();
    }
  }

  function renderExplainResult(data) {
    const box = el("analyze-explain");
    if (!box) return;
    box.innerHTML = "";
    if (!data || data.error) {
      box.textContent = (data && data.error) || "No explanation available.";
      return;
    }

    const head = document.createElement("div");
    head.className = "explain-head";
    const qPill = document.createElement("span");
    qPill.className = "explain-quality explain-quality--" + String(data.quality || "playable");
    qPill.textContent = String(data.quality || "playable");
    head.appendChild(qPill);
    if (typeof data.cp_loss_vs_best === "number" && data.cp_loss_vs_best > 0) {
      const loss = document.createElement("span");
      loss.className = "explain-cp-loss";
      loss.textContent = "−" + data.cp_loss_vs_best + " cp vs best";
      head.appendChild(loss);
    }
    box.appendChild(head);

    if (data.summary) {
      const sum = document.createElement("p");
      sum.className = "explain-summary";
      sum.textContent = data.summary;
      box.appendChild(sum);
    }

    const reasons = data.reasons || [];
    if (reasons.length) {
      const ul = document.createElement("ul");
      ul.className = "explain-reasons";
      reasons.forEach((r) => {
        const li = document.createElement("li");
        li.className = "explain-reason";
        const label = document.createElement("span");
        label.className = "explain-reason-label";
        label.textContent = r.label || r.code;
        const detail = document.createElement("span");
        detail.className = "explain-reason-detail";
        detail.textContent = r.detail ? " — " + r.detail : "";
        li.appendChild(label);
        li.appendChild(detail);
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }

    const warnings = data.warnings || [];
    if (warnings.length) {
      const chips = document.createElement("div");
      chips.className = "explain-warnings";
      warnings.forEach((w) => {
        const chip = document.createElement("span");
        chip.className = "explain-warning-chip";
        chip.title = w.detail || "";
        chip.textContent = w.label || w.code;
        chips.appendChild(chip);
      });
      box.appendChild(chips);
    }
  }

  async function fetchExplain(rootFen, uci) {
    if (explainFetchController) explainFetchController.abort();
    explainFetchController = new AbortController();
    const { signal } = explainFetchController;
    const box = el("analyze-explain");
    if (box) box.innerHTML = '<p class="pv-loading">Explaining…</p>';
    try {
      const res = await fetch("/api/explain_move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fen: rootFen,
          uci,
          root_sims: getAnalysisSims(),
          pv_plies: getPvDepth(),
          strength: getAnalysisStrengthKey(),
        }),
        signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      renderExplainResult(data);
    } catch (e) {
      if (e.name === "AbortError") return;
      if (box) box.textContent = String(e.message || e);
    }
  }

  async function onCandidateClick(uci, liEl) {
    selectedCandidate = uci;
    document.querySelectorAll("#analyze-moves li").forEach((n) => n.classList.remove("candidate-move--selected"));
    liEl.classList.add("candidate-move--selected");
    updatePvDeeperButton();
    const pvBox = el("analyze-pv");
    if (pvFetchController) pvFetchController.abort();
    pvFetchController = new AbortController();
    const { signal } = pvFetchController;
    pvBox.innerHTML = '<p class="pv-loading">Building line…</p>';
    const rootFenForPv = el("analyze-fen").value.trim() || START_FEN;
    // Fire explanation request in parallel; PV continues to render below.
    fetchExplain(rootFenForPv, uci);
    try {
      const res = await fetch("/api/pv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fen: rootFenForPv,
          uci,
          total_plies: getPvDepth(),
          root_sims: getAnalysisSims(),
          fallback_sims: getPvFallbackSims(),
          strength: getAnalysisStrengthKey(),
        }),
        signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      if (data.error) throw new Error(data.error);
      renderPvResult(data, rootFenForPv);
    } catch (e) {
      if (e.name === "AbortError") return;
      pvBox.textContent = String(e.message || e);
      updatePvDeeperButton();
    }
  }

  // ----------------------------- Review tab -----------------------------

  const REVIEW_QUALITY_ORDER = [
    "best",
    "excellent",
    "good",
    "playable",
    "inaccuracy",
    "mistake",
    "blunder",
  ];
  const REVIEW_QUALITY_LABELS = {
    best: "Best",
    excellent: "Excellent",
    good: "Good",
    playable: "Playable",
    inaccuracy: "Inaccuracy",
    mistake: "Mistake",
    blunder: "Blunder",
  };

  let reviewInFlight = null;
  let reviewState = null; // { initial_fen, moves: [...], ply: 0..total }

  function reviewFenAtPly(n) {
    if (!reviewState) return null;
    if (n <= 0) return reviewState.initial_fen;
    const moves = reviewState.moves || [];
    if (n > moves.length) n = moves.length;
    const row = moves[n - 1];
    return row ? row.fen_after : reviewState.initial_fen;
  }

  function reviewTotalPlies() {
    return reviewState && reviewState.moves ? reviewState.moves.length : 0;
  }

  function reviewSetPly(n) {
    if (!reviewState) return;
    const total = reviewTotalPlies();
    if (n < 0) n = 0;
    if (n > total) n = total;
    reviewState.ply = n;
    renderReviewBoard();
    renderReviewNav();
    renderReviewMovelist();
    renderReviewCurrentMove();
  }

  function renderReviewBoard() {
    const host = el("review-board");
    if (!host || !reviewState) return;
    const ply = reviewState.ply;
    const fen = reviewFenAtPly(ply);
    const row = ply > 0 ? reviewState.moves[ply - 1] : null;
    const ft = row ? uciToFromTo(row.uci) : null;
    buildBoard(host, fen, {
      highlightFrom: ft ? ft.from : null,
      highlightTo: ft ? ft.to : null,
    });
  }

  function renderReviewNav() {
    const label = el("review-nav-label");
    const first = el("review-nav-first");
    const prev = el("review-nav-prev");
    const next = el("review-nav-next");
    const last = el("review-nav-last");
    if (!reviewState) return;
    const total = reviewTotalPlies();
    const ply = reviewState.ply;
    if (label) {
      if (ply === 0) {
        label.textContent = `Start · 0 / ${total}`;
      } else {
        const row = reviewState.moves[ply - 1];
        const dot = row.color === "white" ? "." : "…";
        label.textContent = `${row.move_number}${dot} ${row.san} · ${ply} / ${total}`;
      }
    }
    if (first) first.disabled = ply <= 0;
    if (prev) prev.disabled = ply <= 0;
    if (next) next.disabled = ply >= total;
    if (last) last.disabled = ply >= total;
  }

  function renderReviewMovelist() {
    const host = el("review-movelist");
    if (!host || !reviewState) return;
    const moves = reviewState.moves || [];
    const activePly = reviewState.ply;
    host.innerHTML = "";

    // Group into pairs indexed by move number; white always first.
    const pairs = new Map();
    moves.forEach((m, i) => {
      const n = m.move_number;
      if (!pairs.has(n)) pairs.set(n, { num: n, white: null, black: null });
      const bucket = pairs.get(n);
      if (m.color === "white") bucket.white = { row: m, ply: i + 1 };
      else bucket.black = { row: m, ply: i + 1 };
    });

    const nums = [...pairs.keys()].sort((a, b) => a - b);
    for (const n of nums) {
      const bucket = pairs.get(n);

      const numCell = document.createElement("div");
      numCell.className = "review-movelist-num";
      numCell.textContent = `${n}.`;
      host.appendChild(numCell);

      host.appendChild(buildMovelistBtn(bucket.white, activePly));
      host.appendChild(buildMovelistBtn(bucket.black, activePly));
    }

    // Scroll the active button into view.
    const active = host.querySelector(".review-movelist-btn--active");
    if (active && typeof active.scrollIntoView === "function") {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function buildMovelistBtn(entry, activePly) {
    if (!entry) {
      const span = document.createElement("span");
      span.className = "review-movelist-btn review-movelist-btn--empty";
      span.textContent = "…";
      return span;
    }
    const { row, ply } = entry;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "review-movelist-btn" + (ply === activePly ? " review-movelist-btn--active" : "");
    btn.dataset.ply = String(ply);
    btn.title =
      row.quality && REVIEW_QUALITY_LABELS[row.quality]
        ? `${REVIEW_QUALITY_LABELS[row.quality]}${row.cp_loss ? " · −" + row.cp_loss + " cp" : ""}`
        : row.san;

    const dot = document.createElement("span");
    dot.className = "review-movelist-dot";
    dot.dataset.q = row.quality || "";
    btn.appendChild(dot);

    const san = document.createElement("span");
    san.textContent = row.san;
    btn.appendChild(san);

    btn.addEventListener("click", () => reviewSetPly(ply));
    return btn;
  }

  function reviewQualityPillClass(q) {
    return q && REVIEW_QUALITY_LABELS[q] ? `review-quality-pill--${q}` : "";
  }

  function reviewReasonIsNegative(code) {
    if (!code) return false;
    return code.startsWith("misses_");
  }

  function renderReviewCurrentMove() {
    const host = el("review-current-move");
    if (!host || !reviewState) return;
    const ply = reviewState.ply;
    if (ply === 0) {
      host.innerHTML = `<span class="review-current-move-empty">Starting position</span>`;
      return;
    }
    const row = reviewState.moves[ply - 1];
    const moveNumStr = row.color === "white" ? `${row.move_number}.` : `${row.move_number}…`;
    const qLabel = REVIEW_QUALITY_LABELS[row.quality];

    const headBits = [
      `<span class="review-current-move-num">${moveNumStr}</span>`,
      `<span class="review-current-move-san">${reviewEscape(row.san)}</span>`,
    ];
    if (qLabel) {
      headBits.push(
        `<span class="review-quality-pill ${reviewQualityPillClass(row.quality)}">${reviewEscape(qLabel)}</span>`
      );
    }
    if (row.cp_loss) {
      headBits.push(
        `<span class="review-current-move-cploss">−${row.cp_loss} cp vs best</span>`
      );
    }
    if (row.best_san && row.best_san !== row.san) {
      headBits.push(
        `<span class="review-current-move-best">Best: <strong>${reviewEscape(row.best_san)}</strong></span>`
      );
    }

    const summary = row.summary
      ? `<div class="review-current-move-summary">${reviewEscape(row.summary)}</div>`
      : "";

    const reasons = Array.isArray(row.reasons) ? row.reasons : [];
    const reasonsHtml = reasons.length
      ? `<ul class="review-current-move-reasons">${reasons
          .map((r) => {
            const code = r.code || "";
            const neg = reviewReasonIsNegative(code);
            return `
              <li class="review-current-move-reason${neg ? " review-current-move-reason--neg" : ""}">
                <span class="review-current-move-reason-label">${reviewEscape(r.label || "")}</span>
                ${r.detail ? `<span class="review-current-move-reason-detail">${reviewEscape(r.detail)}</span>` : ""}
              </li>`;
          })
          .join("")}</ul>`
      : "";

    const warnings = Array.isArray(row.warnings) ? row.warnings : [];
    const warningsHtml = warnings.length
      ? `<div class="review-current-move-warnings">${warnings
          .map(
            (w) =>
              `<span class="review-current-move-warning-chip" title="${reviewEscape(w.detail || "")}">${reviewEscape(w.label || "")}</span>`
          )
          .join("")}</div>`
      : "";

    let engineBestHtml = "";
    const be = row.best_explain;
    if (be && typeof be === "object") {
      const bsan = reviewEscape(be.san || row.best_san || "");
      const sumB = be.summary ? `<p class="review-engine-best-summary">${reviewEscape(be.summary)}</p>` : "";
      const beReasons = Array.isArray(be.reasons) ? be.reasons : [];
      const beReasonsHtml = beReasons.length
        ? `<ul class="review-engine-best-reasons">${beReasons
            .map(
              (r) => `
              <li class="review-engine-best-reason">
                <span class="review-engine-best-reason-label">${reviewEscape(r.label || "")}</span>
                ${r.detail ? `<span class="review-engine-best-reason-detail">${reviewEscape(r.detail)}</span>` : ""}
              </li>`
            )
            .join("")}</ul>`
        : "";
      const beWarns = Array.isArray(be.warnings) ? be.warnings : [];
      const beWarnsHtml = beWarns.length
        ? `<div class="review-engine-best-warnings">${beWarns
            .map(
              (w) =>
                `<span class="review-engine-best-warn-chip" title="${reviewEscape(w.detail || "")}">${reviewEscape(w.label || "")}</span>`
            )
            .join("")}</div>`
        : "";
      if (row.engine_agrees) {
        engineBestHtml = `
          <div class="review-engine-best review-engine-best--agree">
            <div class="review-engine-best-head">Engine best: <strong>${bsan}</strong>
              <span class="review-engine-best-badge">Same as your move</span>
            </div>
            ${sumB}
          </div>`;
      } else {
        engineBestHtml = `
          <div class="review-engine-best">
            <div class="review-engine-best-head">Stockfish prefers <strong>${bsan}</strong></div>
            ${sumB}
            ${beReasonsHtml}
            ${beWarnsHtml}
          </div>`;
      }
    }

    host.innerHTML = `
      <div class="review-current-move-head">${headBits.join("")}</div>
      ${summary}
      ${reasonsHtml}
      ${warningsHtml}
      ${engineBestHtml}
    `;
  }

  function reviewInitPlayer(data) {
    reviewState = {
      initial_fen: data.initial_fen || "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      moves: data.moves || [],
      ply: 0,
    };
    const playerEl = el("review-player");
    if (playerEl) {
      if (reviewState.moves.length) playerEl.hidden = false;
      else playerEl.hidden = true;
    }
    renderReviewBoard();
    renderReviewNav();
    renderReviewMovelist();
    renderReviewCurrentMove();
  }

  function reviewClearPlayer() {
    reviewState = null;
    const playerEl = el("review-player");
    if (playerEl) playerEl.hidden = true;
  }

  function reviewNavBind() {
    const first = el("review-nav-first");
    const prev = el("review-nav-prev");
    const next = el("review-nav-next");
    const last = el("review-nav-last");
    if (first) first.addEventListener("click", () => reviewSetPly(0));
    if (prev) prev.addEventListener("click", () => reviewState && reviewSetPly(reviewState.ply - 1));
    if (next) next.addEventListener("click", () => reviewState && reviewSetPly(reviewState.ply + 1));
    if (last) last.addEventListener("click", () => reviewState && reviewSetPly(reviewTotalPlies()));

    document.addEventListener("keydown", (ev) => {
      if (!reviewState) return;
      const panel = el("panel-review");
      if (!panel || panel.hidden) return;
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;
      if (ev.key === "ArrowRight") {
        ev.preventDefault();
        reviewSetPly(reviewState.ply + 1);
      } else if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        reviewSetPly(reviewState.ply - 1);
      } else if (ev.key === "Home") {
        ev.preventDefault();
        reviewSetPly(0);
      } else if (ev.key === "End") {
        ev.preventDefault();
        reviewSetPly(reviewTotalPlies());
      }
    });
  }

  function reviewEscape(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  function reviewPillClass(q) {
    if (q === "blunder") return "review-moment-pill--blunder";
    if (q === "mistake") return "review-moment-pill--mistake";
    return "review-moment-pill--inaccuracy";
  }

  function renderReviewQualityBar(quality_counts) {
    const total = REVIEW_QUALITY_ORDER.reduce(
      (acc, k) => acc + (quality_counts[k] || 0),
      0
    );
    if (total <= 0) return "";
    const segs = REVIEW_QUALITY_ORDER.map((k) => {
      const n = quality_counts[k] || 0;
      if (!n) return "";
      const pct = (n / total) * 100;
      const title = `${REVIEW_QUALITY_LABELS[k]}: ${n}`;
      return `<span data-q="${k}" style="width:${pct.toFixed(2)}%" title="${title}"></span>`;
    }).join("");
    const legend = REVIEW_QUALITY_ORDER.filter((k) => quality_counts[k])
      .map((k) => {
        return `<span><span class="q-dot" data-q="${k}" style="background:${reviewLegendColor(k)}"></span>${REVIEW_QUALITY_LABELS[k]} ${quality_counts[k]}</span>`;
      })
      .join("");
    return `<div class="review-quality-bar">${segs}</div><div class="review-quality-legend">${legend}</div>`;
  }

  function reviewLegendColor(q) {
    switch (q) {
      case "best":
      case "excellent":
        return "#43a047";
      case "good":
        return "#7cb342";
      case "playable":
        return "#c0ca33";
      case "inaccuracy":
        return "#fb8c00";
      case "mistake":
        return "#e53935";
      case "blunder":
        return "#880e4f";
      default:
        return "#999";
    }
  }

  function renderReviewThemes(themes) {
    if (!themes || !themes.length) {
      return `<div class="review-empty">No recurring issues detected.</div>`;
    }
    const items = themes
      .map((t) => {
        const count =
          t.count > 1
            ? `<span class="review-theme-count">${t.count} times</span>`
            : `<span class="review-theme-count">once</span>`;
        return `
        <li class="review-theme">
          <div class="review-theme-head">${reviewEscape(t.label)}${count}</div>
          <div>${reviewEscape(t.suggestion)}</div>
        </li>`;
      })
      .join("");
    return `<ul class="review-themes">${items}</ul>`;
  }

  function renderReviewMoments(moments) {
    if (!moments || !moments.length) {
      return `<div class="review-empty">No serious mistakes detected — nice game!</div>`;
    }
    const items = moments
      .map((m) => {
        const pillCls = reviewPillClass(m.quality);
        const moveNum =
          m.color === "white" ? `${m.move_number}.` : `${m.move_number}…`;
        const cpLoss =
          typeof m.cp_loss === "number" && m.cp_loss > 0
            ? `<span class="review-moment-cploss">−${m.cp_loss} cp</span>`
            : "";
        const reasonBits = [];
        if (m.primary_label) reasonBits.push(reviewEscape(m.primary_label));
        if (m.best_san) {
          reasonBits.push(`Best: <strong>${reviewEscape(m.best_san)}</strong>`);
        }
        const reasonLine = reasonBits.length
          ? `<div class="review-moment-reason">${reasonBits.join(" · ")}</div>`
          : "";
        return `
        <li class="review-moment review-moment--clickable" data-ply="${m.ply}" tabindex="0" role="button" title="Jump to this position">
          <div class="review-moment-num">${moveNum}</div>
          <div class="review-moment-main">
            <span class="review-moment-san">${reviewEscape(m.san)}</span>
            <span class="review-moment-pill ${pillCls}">${REVIEW_QUALITY_LABELS[m.quality] || m.quality}</span>
            ${cpLoss}
          </div>
          ${reasonLine}
        </li>`;
      })
      .join("");
    return `<ul class="review-moments">${items}</ul>`;
  }

  function renderReviewSide(color, side) {
    if (!side) return "";
    const title = color === "white" ? "White" : "Black";
    const sub = `${side.moves_analyzed} move${side.moves_analyzed === 1 ? "" : "s"} analyzed`;
    return `
      <div class="review-side-card">
        <div class="review-side-header">
          <h3>${title}</h3>
          <span class="review-side-subtitle">${sub}</span>
        </div>
        <p class="review-side-summary">${reviewEscape(side.summary || "")}</p>
        ${renderReviewQualityBar(side.quality_counts || {})}
        <div class="review-section-title">Recurring themes</div>
        ${renderReviewThemes(side.themes)}
        <div class="review-section-title">Worst moments</div>
        ${renderReviewMoments(side.key_moments)}
      </div>`;
  }

  function renderReviewResults(data) {
    const resultsEl = el("review-results");
    if (!resultsEl) return;
    const headers = data.headers || {};
    const meta = [];
    if (headers.White) meta.push(`<span><strong>White:</strong> ${reviewEscape(headers.White)}</span>`);
    if (headers.Black) meta.push(`<span><strong>Black:</strong> ${reviewEscape(headers.Black)}</span>`);
    if (headers.Result || data.result) {
      meta.push(`<span><strong>Result:</strong> ${reviewEscape(headers.Result || data.result)}</span>`);
    }
    if (data.opening) meta.push(`<span><strong>Opening:</strong> ${reviewEscape(data.opening)}</span>`);
    if (data.total_plies) {
      const suffix = data.truncated ? " (truncated)" : "";
      meta.push(`<span><strong>Plies:</strong> ${data.total_plies}${suffix}</span>`);
    }

    const sides = data.sides || {};
    const sideKeys = Object.keys(sides);
    const sidesClass = sideKeys.length > 1 ? "review-sides review-sides--both" : "review-sides";

    const sidesHtml = sideKeys
      .map((k) => renderReviewSide(k, sides[k]))
      .join("");

    resultsEl.innerHTML = `
      <div class="review-meta">${meta.join("") || "<span>Game review</span>"}</div>
      <div class="${sidesClass}">${sidesHtml}</div>
    `;
  }

  async function runReview() {
    const pgnEl = el("review-pgn");
    const sideEl = el("review-side");
    const depthEl = el("review-depth");
    const runBtn = el("review-run");
    const statusEl = el("review-status");
    const resultsEl = el("review-results");
    if (!pgnEl || !runBtn) return;

    const pgn = (pgnEl.value || "").trim();
    if (!pgn) {
      statusEl.textContent = "Paste a PGN first.";
      statusEl.classList.add("review-status--error");
      return;
    }

    if (reviewInFlight) reviewInFlight.abort();
    const ctrl = new AbortController();
    reviewInFlight = ctrl;

    statusEl.classList.remove("review-status--error");
    statusEl.textContent = "Reviewing game… this can take a minute or two.";
    resultsEl.innerHTML = "";
    reviewClearPlayer();
    runBtn.disabled = true;
    const originalLabel = runBtn.textContent;
    runBtn.textContent = "Reviewing…";

    const body = {
      pgn,
      side: sideEl ? sideEl.value : "both",
      sims_per_move: depthEl ? parseInt(depthEl.value, 10) || 20 : 20,
    };

    try {
      const res = await fetch("/api/review_game", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        throw new Error(data.error || res.statusText || "Review failed.");
      }
      reviewInitPlayer(data);
      renderReviewResults(data);
      bindReviewMomentClicks();
      statusEl.textContent = `Done. Reviewed ${data.total_plies || 0} plies${data.truncated ? " (truncated)" : ""}. Use ← / → to step through moves.`;
    } catch (e) {
      if (e.name === "AbortError") return;
      statusEl.classList.add("review-status--error");
      statusEl.textContent = String(e.message || e);
    } finally {
      if (reviewInFlight === ctrl) reviewInFlight = null;
      runBtn.disabled = false;
      runBtn.textContent = originalLabel;
    }
  }

  function bindReviewMomentClicks() {
    const resultsEl = el("review-results");
    if (!resultsEl) return;
    resultsEl.querySelectorAll(".review-moment--clickable").forEach((node) => {
      const ply = parseInt(node.dataset.ply, 10);
      if (!Number.isFinite(ply)) return;
      node.addEventListener("click", () => reviewSetPly(ply));
      node.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          reviewSetPly(ply);
        }
      });
    });
  }

  function reviewInit() {
    const runBtn = el("review-run");
    if (runBtn) runBtn.addEventListener("click", runReview);
    reviewNavBind();
  }

  function tabsInit() {
    document.querySelectorAll(".folder-tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const panelId = tab.dataset.panel;
        document.querySelectorAll(".folder-tabs .tab").forEach((t) => {
          t.classList.toggle("tab-active", t === tab);
          t.setAttribute("aria-selected", t === tab ? "true" : "false");
        });
        document.querySelectorAll(".folder-body .panel").forEach((p) => {
          const on = p.id === panelId;
          p.toggleAttribute("hidden", !on);
          p.classList.toggle("panel-active", on);
        });
      });
    });
  }

  el("play-color").addEventListener("change", () => {
    humanColorChoice = el("play-color").value;
  });

  function onPlayEloUserChange() {
    if (playEloProgrammatic) return;
    syncPlayEloSliderFromInput();
    patchPlayEngineElo();
  }

  el("play-engine-use-elo").addEventListener("change", () => {
    syncPlayEloControlsDisabled();
    patchPlayEngineElo();
  });
  el("play-engine-elo-slider").addEventListener("input", () => {
    syncPlayEloInputFromSlider();
  });
  el("play-engine-elo-slider").addEventListener("change", onPlayEloUserChange);
  el("play-engine-elo-input").addEventListener("change", onPlayEloUserChange);

  el("btn-new-game").addEventListener("click", newGame);
  el("btn-copy-play-pgn").addEventListener("click", copyPlayPgnToClipboard);
  el("btn-play-to-review").addEventListener("click", openPlayPgnInReview);
  el("btn-analyze").addEventListener("click", runAnalyze);
  el("analyze-strength").addEventListener("change", syncAnalyzeStrengthUI);
  el("analyze-sims").addEventListener("change", () => {
    if (getAnalysisStrengthKey() === "custom") el("analyze-sims").value = String(getAnalysisSims());
  });
  el("pv-depth").addEventListener("change", async () => {
    updatePvDeeperButton();
    if (!selectedCandidate) return;
    const li = document.querySelector("#analyze-moves li.candidate-move--selected");
    if (li) await onCandidateClick(selectedCandidate, li);
  });
  el("pv-deeper").addEventListener("click", async () => {
    const depthSel = el("pv-depth");
    const v = parseInt(depthSel.value, 10);
    if (v >= MAX_PV_PLIES || !selectedCandidate) return;
    depthSel.value = String(Math.min(v + 2, MAX_PV_PLIES));
    const li = document.querySelector("#analyze-moves li.candidate-move--selected");
    if (li) await onCandidateClick(selectedCandidate, li);
  });
  el("analyze-turn").addEventListener("change", () => {
    if (analyzeEditMode && analyzeEditGrid) {
      syncEditorSideUI(el("analyze-turn").value);
      syncAnalyzeFenField();
      renderAnalyzeBoard();
    }
  });
  el("analyze-fen").addEventListener("change", () => {
    lastAnalyzeFen = el("analyze-fen").value.trim() || START_FEN;
    if (!analyzeEditMode) renderAnalyzeBoard();
  });

  el("btn-sync-fen").addEventListener("click", () => {
    if (lastPlayFen) {
      if (analyzeEditMode) exitAnalyzeEditMode(false);
      el("analyze-fen").value = lastPlayFen;
      lastAnalyzeFen = lastPlayFen;
      const parts = lastPlayFen.split(/\s+/);
      if (parts[1] === "w" || parts[1] === "b") el("analyze-turn").value = parts[1];
      renderAnalyzeBoard();
    }
  });

  el("btn-analyze-edit").addEventListener("click", () => {
    if (!analyzeEditMode) enterAnalyzeEditMode();
  });

  el("btn-analyze-startpos").addEventListener("click", () => {
    el("analyze-fen").value = START_FEN;
    lastAnalyzeFen = START_FEN;
    el("analyze-turn").value = "w";
    renderAnalyzeBoard();
  });

  el("btn-analyze-clear").addEventListener("click", () => {
    el("analyze-turn").value = "w";
    el("analyze-fen").value = "8/8/8/8/8/8/8/8 w - - 0 1";
    lastAnalyzeFen = el("analyze-fen").value;
    renderAnalyzeBoard();
  });

  const moveBtn = el("editor-move-btn");
  if (moveBtn) {
    moveBtn.addEventListener("click", () => {
      setEditorTool(null);
    });
  }

  const eraserBtn = el("editor-eraser-btn");
  if (eraserBtn) {
    eraserBtn.addEventListener("click", () => {
      if (analyzeHand && analyzeHand.eraser) {
        setEditorTool(null);
      } else {
        setEditorTool({ eraser: true });
      }
    });
  }

  document.querySelectorAll("#editor-side-segmented .editor-seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      const side = btn.dataset.side === "b" ? "b" : "w";
      if (el("analyze-turn").value === side) return;
      if (analyzeEditMode) pushUndo();
      el("analyze-turn").value = side;
      syncEditorSideUI(side);
      if (analyzeEditMode) {
        syncAnalyzeFenField();
        renderAnalyzeBoard();
      }
    });
  });

  CASTLE_KEYS.forEach((k) => {
    const c = el("editor-castle-" + k);
    if (!c) return;
    c.addEventListener("change", () => {
      if (analyzeEditMode) {
        pushUndo();
        syncAnalyzeFenField();
        renderAnalyzeBoard();
      }
    });
  });

  const editorUndoBtn = el("editor-undo");
  if (editorUndoBtn) {
    editorUndoBtn.addEventListener("click", () => {
      performUndo();
    });
  }

  const editorClearBtn = el("editor-clear");
  if (editorClearBtn) {
    editorClearBtn.addEventListener("click", () => {
      if (!analyzeEditMode) return;
      pushUndo();
      analyzeEditGrid = Array(8).fill(null).map(() => Array(8).fill(null));
      el("analyze-turn").value = "w";
      syncEditorSideUI("w");
      syncEditorCastlingUI("-");
      setEditorTool(null);
      syncAnalyzeFenField();
      renderAnalyzeBoard();
    });
  }

  const editorStartBtn = el("editor-startpos");
  if (editorStartBtn) {
    editorStartBtn.addEventListener("click", () => {
      if (!analyzeEditMode) return;
      pushUndo();
      setEditorGridFromFen(START_FEN);
      setEditorTool(null);
      syncAnalyzeFenField();
      renderAnalyzeBoard();
    });
  }

  const editorCancelBtn = el("editor-cancel");
  if (editorCancelBtn) {
    editorCancelBtn.addEventListener("click", () => {
      if (!analyzeEditMode) return;
      exitAnalyzeEditMode(false);
    });
  }

  const editorDoneBtn = el("editor-done");
  if (editorDoneBtn) {
    editorDoneBtn.addEventListener("click", () => {
      if (!analyzeEditMode) return;
      if (editorDoneBtn.disabled) return;
      exitAnalyzeEditMode(true);
      el("analyze-moves").innerHTML = "";
      const moreRow = el("analyze-moves-more");
      if (moreRow) {
        moreRow.innerHTML = "";
        moreRow.classList.add("hidden");
      }
      el("analyze-pv").innerHTML = "";
      const explainBox2 = el("analyze-explain");
      if (explainBox2) explainBox2.innerHTML = "";
      el("analyze-root-eval").textContent = "";
      selectedCandidate = null;
      updatePvDeeperButton();
    });
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && analyzeEditMode) {
      setEditorTool(null);
      renderAnalyzeBoard();
    }
  });

  const LAUNCH_DISCLAIMER_LS = "ai_chess_hide_launch_disclaimer_v1";

  function launchDisclaimerInit() {
    const root = el("launch-disclaimer");
    const okBtn = el("launch-disclaimer-ok");
    const hideChk = el("launch-disclaimer-hide");
    if (!root || !okBtn) return;
    try {
      if (localStorage.getItem(LAUNCH_DISCLAIMER_LS) === "1") return;
    } catch (_) {
      /* private mode */
    }
    root.removeAttribute("hidden");
    requestAnimationFrame(() => okBtn.focus());

    function close() {
      root.setAttribute("hidden", "");
      try {
        if (hideChk && hideChk.checked) localStorage.setItem(LAUNCH_DISCLAIMER_LS, "1");
      } catch (_) {}
      document.removeEventListener("keydown", onKeyDown);
    }

    function onKeyDown(ev) {
      if (ev.key === "Escape") close();
    }

    okBtn.addEventListener("click", close);
    document.addEventListener("keydown", onKeyDown);
  }

  tabsInit();
  reviewInit();
  launchDisclaimerInit();
  buildPalette();
  el("analyze-fen").value = START_FEN;
  lastAnalyzeFen = START_FEN;
  syncAnalyzeStrengthUI();
  syncPlayEloControlsDisabled();
  updatePvDeeperButton();
  renderPlayBoard();
  renderAnalyzeBoard();

  fetch("/api/health")
    .then((r) => r.json())
    .then((j) => {
      if (!j.engine) console.warn("Stockfish not available:", j.error);
      if (j.play_elo_min && j.play_elo_max) {
        setPlayEloBounds(j.play_elo_min, j.play_elo_max);
      }
    })
    .catch(() => {});

  newGame();
})();
