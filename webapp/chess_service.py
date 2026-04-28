"""
Sessions, FEN helpers, and Stockfish-backed analysis for the Flask UI.

Deployment note: session storage and analysis caches are **process-local** module globals.
Use one worker process or external state if you scale out.
"""
from __future__ import annotations

import copy
import os
import secrets
import sys
from datetime import date
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess_uci as uci
from game import Game
from objects import Color, Move, Outcome
from webapp.stockfish_engine import (
    analysis_limit_from_sims,
    configure_analysis_strength,
    configure_play_elo,
    pv_limit_from_sims,
    stm_score_details,
    with_engine,
    _play_elo_bounds,
)

SESSION_TTL_SEC = int(os.environ.get("CHESS_SESSION_TTL_SEC", "86400"))
PLAY_MATE_ORACLE = os.environ.get("CHESS_PLAY_MATE_ORACLE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

ANALYSIS_CACHE_MAX = int(os.environ.get("CHESS_ANALYSIS_CACHE_SIZE", "32"))
PV_CACHE_MAX = int(os.environ.get("CHESS_PV_CACHE_SIZE", "64"))

_ANALYSIS_LOCK = threading.Lock()
_PV_LOCK = threading.Lock()
_ANALYSIS_CACHE: "OrderedDict[Tuple[str, int], Dict[str, Any]]" = OrderedDict()
_PV_RESPONSE_CACHE: "OrderedDict[Tuple[str, str, int, int, int], Dict[str, Any]]" = OrderedDict()

_REGISTRY_LOCK = threading.Lock()
_SESSION_LOCKS: Dict[str, threading.Lock] = {}


def _lru_set(d: OrderedDict, key, value, max_size: int) -> None:
    if key in d:
        del d[key]
    d[key] = value
    d.move_to_end(key)
    while len(d) > max_size:
        d.popitem(last=False)


def normalize_fen(fen: str) -> str:
    g = Game()
    g.set_fen(fen.strip())
    return g.fen()


def parse_uci_legal(game: Game, uci_s: str) -> Move | None:
    uci_s = uci_s.strip().lower()
    if len(uci_s) < 4:
        return None
    for mv in game.legal_moves():
        if uci.move_to_uci(mv).lower() == uci_s:
            return mv
    return None


def game_from_ucis(start_fen: Optional[str], ucis: List[str]) -> Game:
    g = Game()
    if start_fen and start_fen.strip():
        g.set_fen(start_fen.strip())
    else:
        g.set_fen("startpos")
    for u in ucis:
        mv = parse_uci_legal(g, u)
        if mv is None:
            raise ValueError(f"Illegal UCI in history: {u}")
        ok = g.make_move(mv.start, mv.end, promotion=mv.promo)
        if not ok:
            raise ValueError(f"make_move failed for {u}")
    return g


def parse_engine_elo(raw: Any) -> Optional[int]:
    """``None`` = full Stockfish strength. Otherwise clamp to UCI Elo range."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "max", "full", "none", "null"):
            return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    lo, hi = _play_elo_bounds()
    return max(lo, min(hi, v))


def outcome_to_pgn(res: Optional[Outcome]) -> str:
    if res is None:
        return "*"
    return {
        Outcome.WHITE_WIN: "1-0",
        Outcome.BLACK_WIN: "0-1",
        Outcome.DRAW: "1/2-1/2",
        Outcome.ONGOING: "*",
    }.get(res, "*")


@dataclass
class PlaySession:
    id: str
    human: str  # "white" | "black"
    game: Game
    moves: List[str] = field(default_factory=list)
    start_fen: Optional[str] = None
    touched: float = field(default_factory=time.time)
    engine_elo: Optional[int] = None  # UCI Elo cap for play; None = full strength

    def human_color(self) -> Color:
        return Color.WHITE if self.human == "white" else Color.BLACK


SESSIONS: Dict[str, PlaySession] = {}


@contextmanager
def session_mutex(sid: str):
    with _REGISTRY_LOCK:
        if sid not in _SESSION_LOCKS:
            _SESSION_LOCKS[sid] = threading.Lock()
        lock = _SESSION_LOCKS[sid]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _prune_sessions() -> None:
    if SESSION_TTL_SEC <= 0:
        return
    cutoff = time.time() - float(SESSION_TTL_SEC)
    dead = [sid for sid, s in SESSIONS.items() if s.touched < cutoff]
    for sid in dead:
        del SESSIONS[sid]
    if dead:
        with _REGISTRY_LOCK:
            for sid in dead:
                _SESSION_LOCKS.pop(sid, None)


def new_session(
    human: str,
    start_fen: Optional[str] = None,
    engine_elo: Optional[int] = None,
) -> PlaySession:
    _prune_sessions()
    sid = secrets.token_urlsafe(16)
    g = Game()
    if start_fen and str(start_fen).strip():
        g.set_fen(start_fen.strip())
    else:
        g.set_fen("startpos")
    s = PlaySession(
        id=sid,
        human=human,
        game=g,
        moves=[],
        start_fen=start_fen.strip() if start_fen else None,
        engine_elo=engine_elo,
    )
    SESSIONS[sid] = s
    return s


def get_session(sid: str) -> Optional[PlaySession]:
    _prune_sessions()
    s = SESSIONS.get(sid)
    if s is not None:
        s.touched = time.time()
    return s


def best_move_uci(game: Game, depth: int, engine_elo: Optional[int] = None) -> str:
    if not game.legal_moves():
        return ""
    if PLAY_MATE_ORACLE:
        mates = uci.find_mate_in_one(game)
        if mates:
            return uci.move_to_uci(mates[0])

    board = chess.Board(game.fen())

    def go(eng: chess.engine.SimpleEngine):
        try:
            configure_play_elo(eng, engine_elo)
            res = eng.play(board, chess.engine.Limit(depth=depth))
            return res.move.uci() if res.move else ""
        finally:
            configure_analysis_strength(eng)

    return with_engine(go)


def _multipv_infos(board: chess.Board, limit: chess.engine.Limit, multipv: int) -> List[dict]:
    def go(eng: chess.engine.SimpleEngine):
        configure_analysis_strength(eng)
        return eng.analyse(board, limit, multipv=multipv)

    out = with_engine(go)
    if isinstance(out, list):
        return out
    return [out]


def _analysis_cache_key(nf: str, main_sims: int) -> Tuple[str, int, str]:
    mt = os.environ.get("CHESS_ANALYSIS_MOVETIME_MS", "").strip()
    return (nf, int(main_sims), mt)


def _rank_weights(n: int) -> List[float]:
    w = [2.0 ** (-i) for i in range(n)]
    s = sum(w)
    return [100.0 * x / s for x in w]


def analyze_position(
    game: Game,
    main_sims: int,
    top_k: int,
    display_fen: str,
) -> Dict[str, Any]:
    done, res = game.is_terminal()
    if done:
        return {
            "terminal": True,
            "result": outcome_to_pgn(res),
            "fen": display_fen,
            "root_value": None,
            "moves": [],
        }

    limit = analysis_limit_from_sims(main_sims)
    board = chess.Board(game.fen())
    n_moves = len(list(board.legal_moves))
    k = max(1, min(32, top_k, max(1, n_moves)))
    infos = _multipv_infos(board, limit, k)
    mate_ucis = {uci.move_to_uci(m).lower() for m in uci.find_mate_in_one(game)}

    moves_out: List[Dict[str, Any]] = []
    weights = _rank_weights(len(infos))
    root_val: Optional[float] = None
    root_cp: Optional[int] = None
    root_mate: Optional[int] = None
    root_score_label: Optional[str] = None
    analysis_meta: Dict[str, Any] = {}

    if infos:
        z = infos[0]
        analysis_meta = {
            k_: z.get(k_)
            for k_ in ("depth", "seldepth", "time")
            if k_ in z and z.get(k_) is not None
        }
        if getattr(limit, "depth", None) is not None:
            analysis_meta["limit_depth"] = limit.depth
        if getattr(limit, "time", None) is not None:
            analysis_meta["limit_time_ms"] = int(round(float(limit.time) * 1000.0))

    for i, info in enumerate(infos):
        pv = info.get("pv") or []
        if not pv:
            continue
        m0 = pv[0]
        rel = info["score"].relative
        q, cp_i, mate_i, label = stm_score_details(rel)
        if i == 0:
            root_val = q
            root_cp = cp_i
            root_mate = mate_i
            root_score_label = label
        moves_out.append(
            {
                "uci": m0.uci(),
                "idx": i,
                "rank": i + 1,
                "q": q,
                "cp": cp_i,
                "mate": mate_i,
                "score_label": label,
                "prior": max(1e-6, weights[i] / 100.0),
                "visit_pct": weights[i],
                "mate_in_one": m0.uci().lower() in mate_ucis,
            }
        )

    return {
        "terminal": False,
        "result": None,
        "fen": display_fen,
        "root_value": root_val,
        "root_cp": root_cp,
        "root_mate": root_mate,
        "root_score_label": root_score_label,
        "analysis": analysis_meta,
        "moves": moves_out,
    }


def analyze_fen(fen: str, main_sims: int, top_k: int) -> Dict[str, Any]:
    nf = normalize_fen(fen)
    tree_key = _analysis_cache_key(nf, main_sims)

    with _ANALYSIS_LOCK:
        if tree_key in _ANALYSIS_CACHE:
            entry = _ANALYSIS_CACHE[tree_key]
            _ANALYSIS_CACHE.move_to_end(tree_key)
            g = Game()
            g.set_fen(nf)
            display_fen = entry.get("display_fen", g.fen())
            moves_out = copy.deepcopy(entry["moves"])[: max(1, min(32, top_k))]
            root_val = entry.get("root_value")
            return {
                "terminal": False,
                "result": None,
                "fen": display_fen,
                "root_value": root_val,
                "root_cp": entry.get("root_cp"),
                "root_mate": entry.get("root_mate"),
                "root_score_label": entry.get("root_score_label"),
                "analysis": copy.deepcopy(entry.get("analysis", {})),
                "moves": moves_out,
                "from_cache": True,
            }

    g = Game()
    g.set_fen(nf)
    display_fen = g.fen()
    payload = analyze_position(g, main_sims, top_k, display_fen)

    if not payload.get("terminal"):
        with _ANALYSIS_LOCK:
            _lru_set(
                _ANALYSIS_CACHE,
                tree_key,
                {
                    "display_fen": display_fen,
                    "moves": copy.deepcopy(payload["moves"]),
                    "root_value": payload.get("root_value"),
                    "root_cp": payload.get("root_cp"),
                    "root_mate": payload.get("root_mate"),
                    "root_score_label": payload.get("root_score_label"),
                    "analysis": copy.deepcopy(payload.get("analysis", {})),
                },
                ANALYSIS_CACHE_MAX,
            )

    return {**payload, "from_cache": False}


def pv_line(
    fen: str,
    first_uci: str,
    total_plies: int,
    root_sims: int,
    fallback_sims: int,
) -> Dict[str, Any]:
    nf = normalize_fen(fen)
    uci_l = first_uci.strip().lower()
    mt = os.environ.get("CHESS_ANALYSIS_MOVETIME_MS", "").strip()
    pv_key = (nf, uci_l, max(1, total_plies), int(root_sims), int(fallback_sims), mt)

    with _PV_LOCK:
        if pv_key in _PV_RESPONSE_CACHE:
            _PV_RESPONSE_CACHE.move_to_end(pv_key)
            out = copy.deepcopy(_PV_RESPONSE_CACHE[pv_key])
            out["from_cache"] = True
            return out

    g0 = Game()
    g0.set_fen(nf)
    if g0.is_terminal()[0]:
        return {"line": [], "evals": [], "error": "terminal root", "mode": "none", "from_cache": False}

    mv0 = parse_uci_legal(g0, first_uci)
    if mv0 is None:
        return {"line": [], "evals": [], "error": "illegal first move", "mode": "none", "from_cache": False}

    board = chess.Board(nf)
    try:
        uci_move = chess.Move.from_uci(first_uci.strip())
    except ValueError:
        return {"line": [], "evals": [], "error": "bad uci", "mode": "none", "from_cache": False}

    if uci_move not in board.legal_moves:
        return {"line": [], "evals": [], "error": "illegal first move", "mode": "none", "from_cache": False}

    # Q for the chosen move: prefer multipv line that starts with first_uci
    n_root = len(list(board.legal_moves))
    root_limit = analysis_limit_from_sims(root_sims)
    infos = _multipv_infos(board, root_limit, max(1, min(40, n_root)))
    q_first: Optional[float] = None
    for info in infos:
        pv = info.get("pv") or []
        if pv and pv[0] == uci_move:
            q_first = stm_score_details(info["score"].relative)[0]
            break
    if q_first is None:
        q_first = stm_score_details(infos[0]["score"].relative)[0] if infos else 0.0

    line: List[str] = [uci_move.uci()]
    evals: List[float] = [q_first]

    cont_limit = pv_limit_from_sims(fallback_sims)
    b = board.copy()
    b.push(uci_move)
    for _ in range(max(0, total_plies - 1)):
        if b.is_game_over():
            break

        def one_step(eng: chess.engine.SimpleEngine):
            configure_analysis_strength(eng)
            return eng.analyse(b, cont_limit)

        inf = with_engine(one_step)
        evals.append(stm_score_details(inf["score"].relative)[0])
        pv = inf.get("pv") or []
        if not pv:
            break
        line.append(pv[0].uci())
        b.push(pv[0])

    out = {
        "line": line,
        "evals": evals,
        "mode": "stockfish_pv",
        "pv_origin": "stockfish",
        "description": "Principal variation from Stockfish at the configured depths.",
        "evals_note": "Values are side-to-move evaluations (cp/mate mapped to ~[−1,1]) at each position along the line.",
    }
    with _PV_LOCK:
        _lru_set(_PV_RESPONSE_CACHE, pv_key, copy.deepcopy(out), PV_CACHE_MAX)
    return {**out, "from_cache": False}


def legal_ucis(game: Game) -> List[str]:
    return [uci.move_to_uci(m) for m in game.legal_moves()]


def play_session_to_pgn(sess: PlaySession) -> str:
    """
    Build a PGN string (headers + SAN movetext) from the play session's UCI move list.
    """
    raw_start = (sess.start_fen or "").strip()
    try:
        board = chess.Board(raw_start) if raw_start else chess.Board()
    except ValueError:
        board = chess.Board()

    game = chess.pgn.Game()
    game.headers["Event"] = "Play vs Stockfish"
    game.headers["Site"] = "?"
    game.headers["Date"] = date.today().strftime("%Y.%m.%d")
    hw = sess.human == "white"
    game.headers["White"] = "Human" if hw else "Stockfish"
    game.headers["Black"] = "Stockfish" if hw else "Human"
    done, res = sess.game.is_terminal()
    game.headers["Result"] = outcome_to_pgn(res) if done else "*"

    standard = chess.Board().fen()
    if raw_start and board.fen() != standard:
        game.headers["SetUp"] = "1"
        game.headers["FEN"] = board.fen()

    node = game
    for raw in sess.moves:
        u = (raw or "").strip().lower()
        if len(u) < 4:
            continue
        try:
            move = chess.Move.from_uci(u)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        node = node.add_main_variation(move)
        board.push(move)

    return str(game)


def session_state(sess: PlaySession) -> Dict[str, Any]:
    g = sess.game
    done, res = g.is_terminal()
    fen = g.fen()
    human = sess.human_color()
    your_turn = (not done) and (g.turn == human)
    out: Dict[str, Any] = {
        "fen": fen,
        "moves": list(sess.moves),
        "your_turn": your_turn,
        "terminal": done,
        "result": outcome_to_pgn(res) if done else None,
        "side_to_move": "white" if g.turn == Color.WHITE else "black",
        "legal_ucis": legal_ucis(g) if your_turn else [],
        "engine_elo": sess.engine_elo,
    }
    try:
        out["pgn"] = play_session_to_pgn(sess)
    except Exception:
        out["pgn"] = ""
    return out
