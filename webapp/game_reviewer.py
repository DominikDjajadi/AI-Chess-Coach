"""
Whole-game review.

Takes a PGN string, walks every move, classifies quality against Stockfish's
top choice at each ply, and aggregates per-side feedback (themes, key
moments, simple takeaways).

Reuses the detectors in ``move_explainer`` so the reviewer stays consistent
with the single-move "Why this move" panel. No LLM, no external calls.

Public entry point:
    review_game(pgn_text, side="both", sims_per_move=20, max_plies=None) -> dict

    By default the entire mainline is analyzed end-to-end. Optional ``max_plies``
    truncates early; env ``CHESS_REVIEW_MAX_PLIES`` sets a hard safety ceiling.

Perspective note:
    Stockfish's cp/mate for a position are reported from the side-to-move
    at that position. For each ply we compare the played move's score to the
    best move's score in the same "root STM" frame, so cp_loss is always
    non-negative and measured from the mover's point of view.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import chess_service as svc
from webapp import move_explainer as mx


DEFAULT_SIMS_PER_MOVE = 20
DEFAULT_TOP_K = 8

# Hard safety cap only (pathological PGN). Real games stay under this; override
# with env CHESS_REVIEW_MAX_PLIES if needed.
_DEFAULT_REVIEW_PLIES_CAP = 50_000


_QUALITY_ORDER = ["best", "excellent", "good", "playable", "inaccuracy", "mistake", "blunder"]

# Which move codes are worth aggregating as a coaching theme, and how to
# phrase the takeaway. Keys are reason / warning codes emitted by
# ``move_explainer``.
_THEME_LABELS: Dict[str, Dict[str, str]] = {
    # Warnings — things the player keeps doing wrong.
    "hangs_piece": {
        "label": "Hanging pieces",
        "suggestion": "Before each move, check the destination square: can a cheaper opponent piece capture it for free?",
    },
    "weakens_king_safety": {
        "label": "Loosening king safety",
        "suggestion": "Be careful pushing pawns in front of your castled king; they rarely return to defend.",
    },
    "blocks_own_piece": {
        "label": "Blocking your own pieces",
        "suggestion": "Watch where you place pieces relative to your own bishops and rooks — don't cut off their lines.",
    },
    "loses_tempo_if_obvious": {
        "label": "Pieces kicked by pawns",
        "suggestion": "Don't put pieces where a pawn can easily chase them — pick squares pawns can't attack cheaply.",
    },
    "allows_fork": {
        "label": "Allowing opponent forks",
        "suggestion": "Scan for your opponent's knight and queen jumps; forks are the easiest tactic to miss.",
    },
    # Missed tactical ideas.
    "misses_mate_in_one": {
        "label": "Missing forced mates",
        "suggestion": "Always scan every check in the position first — forced mates hide there.",
    },
    "misses_winning_capture": {
        "label": "Missing free material",
        "suggestion": "Calculate every capture before settling on a move; don't overlook undefended pieces.",
    },
    "misses_fork": {
        "label": "Missing your own forks",
        "suggestion": "Look for squares where one of your pieces attacks two enemy pieces at once — especially knight outposts.",
    },
    "misses_check": {
        "label": "Missing strong checks",
        "suggestion": "Forcing checks often lead to tactics; consider every check before quieter moves.",
    },
}


def _parse_pgn(pgn_text: str) -> Optional[chess.pgn.Game]:
    if not pgn_text or not pgn_text.strip():
        return None
    try:
        return chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:
        return None


def _empty_side_stats() -> Dict[str, Any]:
    return {
        "moves_analyzed": 0,
        "quality_counts": {k: 0 for k in _QUALITY_ORDER},
        "total_cp_loss": 0,
        "themes_raw": {},  # code -> count
        "key_moments": [],  # list of dicts
    }


def _quality_rank(q: str) -> int:
    try:
        return _QUALITY_ORDER.index(q)
    except ValueError:
        return 0


def _classify_side(side_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Build the public per-side summary from raw counters."""
    n = side_stats["moves_analyzed"]
    total_loss = side_stats["total_cp_loss"]
    acpl = int(round(total_loss / n)) if n > 0 else 0

    # Themes: sort by count desc, keep at most 4.
    themes: List[Dict[str, Any]] = []
    for code, count in sorted(side_stats["themes_raw"].items(), key=lambda kv: -kv[1]):
        if count < 1:
            continue
        meta = _THEME_LABELS.get(code)
        if meta is None:
            continue
        themes.append(
            {
                "code": code,
                "count": count,
                "label": meta["label"],
                "suggestion": meta["suggestion"],
            }
        )
        if len(themes) >= 4:
            break

    # Key moments: pick worst cp_loss moves, tie-break by quality rank.
    km = sorted(
        side_stats["key_moments"],
        key=lambda m: (-m.get("cp_loss", 0), -_quality_rank(m.get("quality", "best"))),
    )
    key_moments = km[:5]

    headline = _side_headline(acpl, side_stats["quality_counts"], themes)

    return {
        "moves_analyzed": n,
        "acpl": acpl,
        "quality_counts": side_stats["quality_counts"],
        "themes": themes,
        "key_moments": key_moments,
        "summary": headline,
    }


def _side_headline(
    acpl: int,
    quality_counts: Dict[str, int],
    themes: List[Dict[str, Any]],
) -> str:
    blunders = quality_counts.get("blunder", 0)
    mistakes = quality_counts.get("mistake", 0)
    inaccs = quality_counts.get("inaccuracy", 0)

    # Accuracy-ish bucket based on average centipawn loss.
    if acpl < 20:
        grade = "very accurate play"
    elif acpl < 50:
        grade = "solid play"
    elif acpl < 100:
        grade = "shaky in places"
    elif acpl < 200:
        grade = "several inaccurate moves"
    else:
        grade = "many costly mistakes"

    mistake_bits: List[str] = []
    if blunders:
        mistake_bits.append(f"{blunders} blunder{'s' if blunders != 1 else ''}")
    if mistakes:
        mistake_bits.append(f"{mistakes} mistake{'s' if mistakes != 1 else ''}")
    if inaccs and not mistake_bits:
        mistake_bits.append(f"{inaccs} inaccuracies")

    pieces = [f"Overall: {grade} (avg loss ≈ {acpl} cp)"]
    if mistake_bits:
        pieces.append("; ".join(mistake_bits))
    if themes:
        pieces.append(f"recurring issue: {themes[0]['label'].lower()}")

    return " — ".join(pieces) + "."


def _opening_name_from_headers(game: chess.pgn.Game) -> Optional[str]:
    for key in ("Opening", "ECO"):
        val = game.headers.get(key)
        if val and val.strip():
            return val.strip()
    return None


def _collect_theme_codes(reasons: List[dict], warnings: List[dict]) -> List[str]:
    """Return codes from reasons/warnings that are worth aggregating."""
    out: List[str] = []
    for r in reasons:
        code = r.get("code")
        if code in _THEME_LABELS:
            out.append(code)
    for w in warnings:
        code = w.get("code")
        if code in _THEME_LABELS:
            out.append(code)
    return out


def _explain_move_features(
    board: chess.Board,
    move: chess.Move,
    side: chess.Color,
) -> Dict[str, Any]:
    """
    Rule-based summary and bullets for *any* legal move — used to describe
    why Stockfish's best move is good without a second engine query.
    """
    try:
        san = board.san(move)
    except Exception:
        san = move.uci()
    after = board.copy()
    after.push(move)
    positives = mx._collect_positive_reasons(board, move, after, side)
    warnings: List[dict] = []
    w = mx._warn_hangs_piece(move, after, side)
    if w:
        warnings.append(w)
    w = mx._warn_weakens_king_safety(board, move, after, side)
    if w:
        warnings.append(w)
    w = mx._warn_blocks_own_piece(board, move, after, side)
    if w:
        warnings.append(w)
    w = mx._warn_loses_tempo(board, move, after, side)
    if w:
        warnings.append(w)
    w = mx._warn_allows_fork(after, side)
    if w:
        warnings.append(w)

    summary = mx._summarize(san, "best", positives, [], warnings)
    seen: set = set()
    reasons_out: List[dict] = []
    for rr in positives:
        c = rr.get("code")
        if not c or c in seen:
            continue
        seen.add(c)
        reasons_out.append(rr)
    reasons_out = reasons_out[:5]
    return {
        "san": san,
        "summary": summary,
        "reasons": reasons_out,
        "warnings": warnings,
    }


def _analyze_ply(
    board: chess.Board,
    played: chess.Move,
    sims_per_move: int,
) -> Dict[str, Any]:
    """
    Run one ply of game review. Returns a small dict with just what the
    aggregator needs. Mirrors a trimmed-down ``move_explainer.explain_move``
    (no PV generation, no SAN/fen echo).
    """
    try:
        root = svc.analyze_fen(board.fen(), max(1, sims_per_move), DEFAULT_TOP_K)
    except Exception as e:
        return {"error": f"analyze failed: {e}"}

    root_moves: List[Dict[str, Any]] = root.get("moves") or []
    if not root_moves:
        return {"error": "no engine output"}

    best_info = root_moves[0]
    best_uci = str(best_info.get("uci", "")).lower()
    best_cp = best_info.get("cp")
    best_mate = best_info.get("mate")
    best_cp_norm = mx._score_to_cp(best_cp, best_mate)

    # Find the played move in the top_k list; otherwise run a small secondary
    # analysis on the resulting position.
    played_uci = played.uci().lower()
    cand_info: Optional[Dict[str, Any]] = None
    for m in root_moves:
        if str(m.get("uci", "")).lower() == played_uci:
            cand_info = m
            break

    if cand_info is not None:
        cand_cp = cand_info.get("cp")
        cand_mate = cand_info.get("mate")
    else:
        cand_cp = None
        cand_mate = None
        try:
            board_after_tmp = board.copy()
            board_after_tmp.push(played)
            limit = svc.analysis_limit_from_sims(max(1, sims_per_move))

            def _go(eng):
                svc.configure_analysis_strength(eng)
                return eng.analyse(board_after_tmp, limit)

            info = svc.with_engine(_go)
            rel = info["score"].relative
            if rel.is_mate():
                mv_m = rel.mate()
                cand_mate = -mv_m if mv_m is not None else None
            else:
                cp = rel.score()
                cand_cp = -cp if cp is not None else 0
        except Exception:
            pass

    cand_cp_norm = mx._score_to_cp(cand_cp, cand_mate)
    cp_loss = max(0, best_cp_norm - cand_cp_norm)
    quality = mx._classify_quality(cp_loss, best_mate, cand_mate)

    # Feature extraction — the same helpers the live explainer uses.
    board_after = board.copy()
    board_after.push(played)
    side = board.turn

    positives = mx._collect_positive_reasons(board, played, board_after, side)

    best_move_obj: Optional[chess.Move] = None
    best_san: Optional[str] = None
    if best_uci:
        try:
            bm = chess.Move.from_uci(best_uci)
            if bm in board.legal_moves:
                best_move_obj = bm
                best_san = board.san(bm)
        except ValueError:
            pass

    engine_agrees = bool(best_uci and played_uci == best_uci)

    cand_codes = {r["code"] for r in positives}
    missed = mx._missed_reasons(board, best_move_obj, cand_codes, "", side)

    warnings: List[dict] = []
    w = mx._warn_hangs_piece(played, board_after, side)
    if w:
        warnings.append(w)
    w = mx._warn_weakens_king_safety(board, played, board_after, side)
    if w:
        warnings.append(w)
    w = mx._warn_blocks_own_piece(board, played, board_after, side)
    if w:
        warnings.append(w)
    w = mx._warn_loses_tempo(board, played, board_after, side)
    if w:
        warnings.append(w)
    w = mx._warn_allows_fork(board_after, side)
    if w:
        warnings.append(w)

    # Pick the single most salient reason for the key-moments view:
    # for bad moves prefer a missed idea, then a warning; for good moves
    # take the strongest positive reason.
    if quality in mx._BAD_QUALITIES and missed:
        primary = missed[0]
    elif quality in mx._BAD_QUALITIES and warnings:
        primary = warnings[0]
    elif positives:
        primary = positives[0]
    elif warnings:
        primary = warnings[0]
    else:
        primary = None

    if quality in mx._BAD_QUALITIES:
        reasons_ordered = missed + positives
    else:
        reasons_ordered = positives + missed

    # Same dedup + cap the single-move explainer uses so the UI matches.
    seen: set = set()
    reasons_final: List[dict] = []
    for rr in reasons_ordered:
        c = rr.get("code")
        if not c or c in seen:
            continue
        seen.add(c)
        reasons_final.append(rr)
    reasons_final = reasons_final[:5]

    best_explain: Optional[Dict[str, Any]] = None
    if best_move_obj is not None:
        best_explain = _explain_move_features(board, best_move_obj, side)

    return {
        "cp_loss": int(cp_loss),
        "best_cp_norm": int(best_cp_norm),
        "cand_cp_norm": int(cand_cp_norm),
        "quality": quality,
        "best_uci": best_uci,
        "best_san": best_san,
        "engine_agrees": engine_agrees,
        "best_explain": best_explain,
        "positives": positives,
        "missed": missed,
        "reasons": reasons_final,
        "warnings": warnings,
        "primary": primary,
    }


def review_game(
    pgn_text: str,
    side: str = "both",
    sims_per_move: int = DEFAULT_SIMS_PER_MOVE,
    max_plies: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Review every ply of a PGN game and return per-side feedback.

    ``side`` filters which sides get detailed feedback: ``"both"`` (default),
    ``"white"``, or ``"black"``. The opposing side's moves are still analyzed
    at a shallow level so we can compute the played-move eval for the side we
    care about, but their stats aren't aggregated when filtered.
    """
    game = _parse_pgn(pgn_text)
    if game is None:
        return {"error": "Could not parse PGN."}

    board = game.board()
    initial_fen = board.fen()
    moves_iter = list(game.mainline_moves())
    if not moves_iter:
        return {"error": "PGN has no moves."}

    sims_per_move = max(4, min(int(sims_per_move or DEFAULT_SIMS_PER_MOVE), 96))

    safety = max(100, int(os.environ.get("CHESS_REVIEW_MAX_PLIES", str(_DEFAULT_REVIEW_PLIES_CAP))))
    original_n = len(moves_iter)
    cap = safety
    if max_plies is not None:
        cap = min(max(1, int(max_plies)), safety)
    truncated = original_n > cap
    if truncated:
        moves_iter = moves_iter[:cap]

    want_white = side in ("both", "white")
    want_black = side in ("both", "black")

    stats: Dict[str, Dict[str, Any]] = {
        "white": _empty_side_stats(),
        "black": _empty_side_stats(),
    }

    move_rows: List[Dict[str, Any]] = []

    for ply_index, played in enumerate(moves_iter, start=1):
        mover_color = board.turn  # whose turn before the move
        mover_name = "white" if mover_color == chess.WHITE else "black"
        record_side = (mover_color == chess.WHITE and want_white) or (
            mover_color == chess.BLACK and want_black
        )

        try:
            san = board.san(played)
        except Exception:
            san = played.uci()

        fen_before = board.fen()
        move_number = (ply_index + 1) // 2  # 1-based full move number

        if not record_side:
            board.push(played)
            move_rows.append(
                {
                    "ply": ply_index,
                    "move_number": move_number,
                    "color": mover_name,
                    "san": san,
                    "uci": played.uci(),
                    "quality": "skipped",
                    "cp_loss": 0,
                    "best_cp_norm": move_rows[-1].get("best_cp_norm") if move_rows else None,
                    "fen_before": fen_before,
                    "fen_after": board.fen(),
                }
            )
            continue

        ply = _analyze_ply(board, played, sims_per_move=sims_per_move)
        if ply.get("error"):
            board.push(played)
            move_rows.append(
                {
                    "ply": ply_index,
                    "move_number": move_number,
                    "color": mover_name,
                    "san": san,
                    "uci": played.uci(),
                    "quality": "error",
                    "cp_loss": 0,
                    "best_cp_norm": None,
                    "fen_before": fen_before,
                    "fen_after": board.fen(),
                    "error": ply["error"],
                }
            )
            continue

        ss = stats[mover_name]
        ss["moves_analyzed"] += 1
        ss["quality_counts"][ply["quality"]] = ss["quality_counts"].get(ply["quality"], 0) + 1
        ss["total_cp_loss"] += ply["cp_loss"]

        for code in _collect_theme_codes(ply["reasons"], ply["warnings"]):
            ss["themes_raw"][code] = ss["themes_raw"].get(code, 0) + 1

        primary = ply.get("primary") or {}
        primary_label = primary.get("label") if isinstance(primary, dict) else None
        primary_code = primary.get("code") if isinstance(primary, dict) else None

        moment = {
            "ply": ply_index,
            "move_number": move_number,
            "color": mover_name,
            "san": san,
            "uci": played.uci(),
            "quality": ply["quality"],
            "cp_loss": ply["cp_loss"],
            "best_san": ply.get("best_san"),
            "best_uci": ply.get("best_uci"),
            "primary_code": primary_code,
            "primary_label": primary_label,
            "fen_before": fen_before,
        }
        # We only highlight "key moments" when something actually went wrong
        # (inaccuracy or worse). Strong moves are not surfaced here — the
        # summary already reports good play in aggregate.
        if ply["quality"] in ("inaccuracy", "mistake", "blunder"):
            ss["key_moments"].append(moment)

        board.push(played)

        summary_line = mx._summarize(
            san,
            ply["quality"],
            ply.get("positives") or [],
            ply.get("missed") or [],
            ply.get("warnings") or [],
        )

        move_rows.append(
            {
                "ply": ply_index,
                "move_number": move_number,
                "color": mover_name,
                "san": san,
                "uci": played.uci(),
                "quality": ply["quality"],
                "cp_loss": ply["cp_loss"],
                "best_cp_norm": ply.get("best_cp_norm"),
                "best_san": ply.get("best_san"),
                "engine_agrees": ply.get("engine_agrees"),
                "best_explain": ply.get("best_explain"),
                "summary": summary_line,
                "reasons": ply.get("reasons") or [],
                "warnings": ply.get("warnings") or [],
                "fen_before": fen_before,
                "fen_after": board.fen(),
            }
        )

    headers = {
        k: game.headers.get(k, "")
        for k in ("White", "Black", "Result", "Event", "Date", "Opening", "ECO", "TimeControl")
        if game.headers.get(k)
    }

    sides_out: Dict[str, Any] = {}
    if want_white:
        sides_out["white"] = _classify_side(stats["white"])
    if want_black:
        sides_out["black"] = _classify_side(stats["black"])

    return {
        "headers": headers,
        "result": game.headers.get("Result", ""),
        "initial_fen": initial_fen,
        "total_plies": len(moves_iter),
        "truncated": truncated,
        "sims_per_move": sims_per_move,
        "sides": sides_out,
        "moves": move_rows,
        "opening": _opening_name_from_headers(game),
    }
