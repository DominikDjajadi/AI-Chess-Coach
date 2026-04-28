"""
Process-local Stockfish instance for the Flask app (UCI via python-chess).

Set ``CHESS_STOCKFISH_PATH`` to the engine executable if it is not on ``PATH``.
Engine calls are serialized with a lock (UCI is not thread-safe).

Analysis strength / accuracy (env):

- ``CHESS_STOCKFISH_THREADS`` — UCI Threads (default 2).
- ``CHESS_STOCKFISH_HASH_MB`` — UCI Hash in MB (default 128).
- ``CHESS_SYZYGY_PATH`` — optional Syzygy tablebase path for endgames.
- ``CHESS_ANALYSIS_SIMS_DIVISOR`` — maps UI ``sims`` to depth via ``sims // divisor``
  (default 2; larger = weaker but faster). Ignored when ``CHESS_ANALYSIS_MOVETIME_MS`` is set.
- ``CHESS_ANALYSIS_MOVETIME_MS`` — if set, analyse/play uses this **time limit** (ms) instead of depth
  (stronger real-world accuracy for a fixed budget; depth is not capped the same way).
- ``CHESS_ANALYSIS_MIN_DEPTH`` / ``CHESS_ANALYSIS_MAX_DEPTH`` — clamp depth from sims (defaults 6 / 64).
"""
from __future__ import annotations

import atexit
import math
import os
import threading
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

import chess
import chess.engine

T = TypeVar("T")

_engine: Optional[chess.engine.SimpleEngine] = None
_lock = threading.Lock()
_configured = False


def stockfish_path() -> str:
    p = os.environ.get("CHESS_STOCKFISH_PATH", "").strip()
    return p if p else "stockfish"


def _shutdown() -> None:
    global _engine, _configured
    with _lock:
        if _engine is not None:
            try:
                _engine.quit()
            except Exception:
                pass
            _engine = None
        _configured = False


def _configure_engine(eng: chess.engine.SimpleEngine) -> None:
    global _configured
    if _configured:
        return
    opts: Dict[str, Any] = {}
    threads = int(os.environ.get("CHESS_STOCKFISH_THREADS", "2"))
    hash_mb = int(os.environ.get("CHESS_STOCKFISH_HASH_MB", "128"))
    if threads > 0:
        opts["Threads"] = threads
    if hash_mb > 0:
        opts["Hash"] = hash_mb
    syzygy = os.environ.get("CHESS_SYZYGY_PATH", "").strip()
    if syzygy:
        opts["SyzygyPath"] = syzygy
    if opts:
        eng.configure(opts)
    _configured = True


def _ensure_engine() -> chess.engine.SimpleEngine:
    global _engine
    if _engine is None:
        _engine = chess.engine.SimpleEngine.popen_uci(stockfish_path())
        _configure_engine(_engine)
        atexit.register(_shutdown)
    return _engine


def with_engine(fn: Callable[[chess.engine.SimpleEngine], T]) -> T:
    with _lock:
        eng = _ensure_engine()
        return fn(eng)


# Stockfish's documented UCI_Elo bounds (see stockfish/src/search.h).
# Values outside this range are silently rejected by the engine, so we use
# them as hard fallbacks when the live engine hasn't been queried yet.
_SF_ELO_FLOOR = 1320
_SF_ELO_CEIL = 3190


def _play_elo_bounds() -> Tuple[int, int]:
    lo = int(os.environ.get("CHESS_PLAY_ELO_MIN", str(_SF_ELO_FLOOR)))
    hi = int(os.environ.get("CHESS_PLAY_ELO_MAX", str(_SF_ELO_CEIL)))
    return (min(lo, hi), max(lo, hi))


def _engine_elo_bounds(eng: chess.engine.SimpleEngine) -> Tuple[int, int]:
    """
    Ask the live engine for its actual UCI_Elo range. Falls back to the
    documented Stockfish bounds if the engine didn't advertise them.
    """
    lo = _SF_ELO_FLOOR
    hi = _SF_ELO_CEIL
    try:
        opt = eng.options.get("UCI_Elo")
        if opt is not None:
            if opt.min is not None:
                lo = int(opt.min)
            if opt.max is not None:
                hi = int(opt.max)
    except Exception:
        pass
    env_lo, env_hi = _play_elo_bounds()
    # Respect env overrides when they land inside the engine's range.
    lo = max(lo, env_lo)
    hi = min(hi, env_hi)
    if lo > hi:
        lo, hi = env_lo, env_hi
    return lo, hi


def engine_play_elo_bounds() -> Tuple[int, int]:
    """Public wrapper: return the usable UCI_Elo range for the running engine."""
    def q(eng: chess.engine.SimpleEngine) -> Tuple[int, int]:
        return _engine_elo_bounds(eng)

    try:
        return with_engine(q)
    except Exception:
        return _play_elo_bounds()


def configure_analysis_strength(eng: chess.engine.SimpleEngine) -> None:
    """Full engine strength (analysis / PV). Disables UCI Elo handicap."""
    try:
        eng.configure({"UCI_LimitStrength": False})
    except Exception:
        pass


def configure_play_elo(eng: chess.engine.SimpleEngine, elo: Optional[int]) -> None:
    """
    For play mode: limit strength to approximate human ``elo``, or full strength if ``elo`` is None.
    Uses Stockfish UCI ``UCI_LimitStrength`` + ``UCI_Elo`` when available.

    If the requested ``elo`` falls outside the engine's actual ``UCI_Elo``
    range we clamp into that range rather than silently falling back to full
    strength — Stockfish would otherwise reject e.g. ``UCI_Elo=600`` and the
    user would be facing a full-strength engine thinking they'd asked for a
    beginner.
    """
    if elo is None:
        configure_analysis_strength(eng)
        return
    lo, hi = _engine_elo_bounds(eng)
    v = max(lo, min(hi, int(elo)))
    try:
        eng.configure({"UCI_LimitStrength": True, "UCI_Elo": v})
    except Exception:
        # Last-ditch: try the engine floor, then give up to full strength.
        try:
            eng.configure({"UCI_LimitStrength": True, "UCI_Elo": lo})
        except Exception:
            configure_analysis_strength(eng)


def relative_score_to_q(rel: chess.engine.Score) -> float:
    """Map python-chess side-to-move score to roughly [-1, 1] for the UI (legacy)."""
    if rel.is_mate():
        m = rel.mate()
        if m is None:
            return 0.0
        if m > 0:
            return max(-1.0, min(1.0, 1.0 - 1.0 / (m + 1.0)))
        return max(-1.0, min(1.0, -1.0 + 1.0 / (-m + 1.0)))
    cp = rel.score()
    if cp is None:
        return 0.0
    return max(-1.0, min(1.0, math.tanh(cp / 450.0)))


def stm_score_details(rel: chess.engine.Score) -> Tuple[float, Optional[int], Optional[int], str]:
    """
    Side-to-move evaluation: returns (q, cp_centipawns, mate_plies, label).

    ``cp_centipawns`` is None when the score is mate; ``mate_plies`` is signed
    (winning mate positive, losing negative) per python-chess.
    ``label`` is a short human string (pawns or mate).
    """
    q = relative_score_to_q(rel)
    if rel.is_mate():
        m = rel.mate()
        if m is None:
            return q, None, None, "—"
        # Winning mate: show +M<n>; losing: -M<n>
        if m > 0:
            return q, None, m, f"+M{m}"
        return q, None, m, f"-M{abs(m)}"
    cp = rel.score()
    if cp is None:
        return q, 0, None, "0.00"
    pawns = cp / 100.0
    label = f"{pawns:+.2f}"
    return q, cp, None, label


def play_depth() -> int:
    d = int(os.environ.get("CHESS_STOCKFISH_DEPTH", "14"))
    return max(1, min(64, d))


def analysis_depth_from_sims(sims: int) -> int:
    """Map UI ``sims`` knob to Stockfish depth (legacy field name ``sims``)."""
    s = max(1, int(sims))
    div = max(1, int(os.environ.get("CHESS_ANALYSIS_SIMS_DIVISOR", "2")))
    dmin = max(1, int(os.environ.get("CHESS_ANALYSIS_MIN_DEPTH", "6")))
    dmax = max(dmin, int(os.environ.get("CHESS_ANALYSIS_MAX_DEPTH", "64")))
    return max(dmin, min(dmax, s // div))


def pv_fallback_depth_from_sims(sims: int) -> int:
    s = max(1, int(sims))
    div = max(1, int(os.environ.get("CHESS_ANALYSIS_SIMS_DIVISOR", "2")))
    # Continuation lines: slightly shallower than root multipv by default
    cap = max(4, int(os.environ.get("CHESS_PV_MAX_DEPTH", "40")))
    return max(4, min(cap, s // div))


def analysis_limit_from_sims(sims: int) -> chess.engine.Limit:
    """
    Search limit for analysis. If ``CHESS_ANALYSIS_MOVETIME_MS`` is set, use fixed time
    (wall-clock budget); otherwise use depth from ``analysis_depth_from_sims``.
    """
    mt = os.environ.get("CHESS_ANALYSIS_MOVETIME_MS", "").strip()
    if mt:
        sec = max(0.05, int(mt) / 1000.0)
        return chess.engine.Limit(time=sec)
    return chess.engine.Limit(depth=analysis_depth_from_sims(sims))


def pv_limit_from_sims(sims: int) -> chess.engine.Limit:
    """Limit for PV continuation steps (matches analysis when using depth)."""
    mt = os.environ.get("CHESS_ANALYSIS_MOVETIME_MS", "").strip()
    if mt:
        sec = max(0.05, int(mt) / 1000.0)
        # Shorter than root so PV lines stay snappy when using movetime
        sec = max(0.03, sec * 0.4)
        return chess.engine.Limit(time=sec)
    return chess.engine.Limit(depth=pv_fallback_depth_from_sims(sims))
