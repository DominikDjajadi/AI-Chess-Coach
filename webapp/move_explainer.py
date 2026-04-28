"""
Rule-based move explanation.

Produces a human-readable explanation of why a candidate chess move is good
or bad at a given position, using only deterministic board features and
the existing Stockfish-backed analysis pipeline. No LLM, no external API.

Public entry point:
    explain_move(fen, uci, root_sims=64, pv_plies=6) -> dict

Perspective note:
    Stockfish multi-PV ``score.relative`` is reported from the side-to-move
    at the analyzed position. All candidate moves from ``/api/analyze`` share
    the same root position, so their cp values are already on the same
    "root side-to-move" axis and can be compared directly.
    ``cp_before`` is the root eval (best-move's STM cp); ``cp_after_move`` is
    the engine cp for the candidate line, still in root-STM perspective.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chess

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import chess_service as svc


PIECE_VALUES: Dict[int, int] = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}
PIECE_NAMES: Dict[int, str] = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}
CENTER_SQUARES = frozenset({chess.D4, chess.E4, chess.D5, chess.E5})
EXTENDED_CENTER = frozenset(
    {
        chess.C3, chess.D3, chess.E3, chess.F3,
        chess.C4, chess.D4, chess.E4, chess.F4,
        chess.C5, chess.D5, chess.E5, chess.F5,
        chess.C6, chess.D6, chess.E6, chess.F6,
    }
)

# Mate-to-cp conversion keeps a linear ordering so cp_loss math works for
# mate scores. The exact magnitude is not an engine eval — it's a comparator.
_MATE_CP_BASE = 100_000


def _score_to_cp(cp: Optional[int], mate: Optional[int]) -> int:
    """Collapse engine score (cp or mate) into a single comparable cp value."""
    if mate is not None and mate != 0:
        sign = 1 if mate > 0 else -1
        return sign * (_MATE_CP_BASE - abs(mate) * 100)
    return int(cp or 0)


def _classify_quality(cp_loss: int, mate_before: Optional[int], mate_after: Optional[int]) -> str:
    """Bucket the cp loss (root-STM) into a quality label.

    Mate-aware: if the root has a winning mate and the candidate fails to
    keep one, it's at least a mistake regardless of cp numbers.
    """
    if cp_loss <= 0:
        return "best"
    if mate_before is not None and mate_before > 0 and (mate_after is None or mate_after <= 0):
        return "blunder" if cp_loss > 400 else "mistake"
    if cp_loss <= 20:
        return "excellent"
    if cp_loss <= 50:
        return "good"
    if cp_loss <= 100:
        return "playable"
    if cp_loss <= 200:
        return "inaccuracy"
    if cp_loss <= 400:
        return "mistake"
    return "blunder"


# -------------------- Static Exchange Evaluation (approx) --------------------

def _least_valuable_attacker(board: chess.Board, to_sq: int, side: chess.Color) -> Optional[int]:
    best_sq: Optional[int] = None
    best_val = 10**9
    for sq in board.attackers(side, to_sq):
        p = board.piece_at(sq)
        if p is None:
            continue
        v = PIECE_VALUES[p.piece_type]
        if v < best_val:
            best_val = v
            best_sq = sq
    return best_sq


def _see(board: chess.Board, to_sq: int, attacker_sq: int) -> int:
    """
    Approximate SEE: value gained by the side whose piece sits on attacker_sq
    if they initiate a capture sequence on to_sq.

    Does not handle X-rays / batteries precisely but is fine for heuristic
    "is this piece safe?" questions.
    """
    b = board.copy(stack=False)
    attacker = b.piece_at(attacker_sq)
    if attacker is None:
        return 0
    side = attacker.color

    captured = b.piece_at(to_sq)
    gains: List[int] = [PIECE_VALUES[captured.piece_type] if captured else 0]

    # Move the initial attacker to the target square.
    b.remove_piece_at(attacker_sq)
    b.set_piece_at(to_sq, attacker)
    piece_on_target_val = PIECE_VALUES[attacker.piece_type]

    cur = not side
    while True:
        lva_sq = _least_valuable_attacker(b, to_sq, cur)
        if lva_sq is None:
            break
        lva = b.piece_at(lva_sq)
        if lva is None:
            break
        gains.append(piece_on_target_val - gains[-1])
        b.remove_piece_at(lva_sq)
        b.set_piece_at(to_sq, lva)
        piece_on_target_val = PIECE_VALUES[lva.piece_type]
        cur = not cur

    # Canonical SEE backward propagation: at each ply the side to move may
    # "stand pat" (stop) instead of recapturing if continuing is worse.
    for i in range(len(gains) - 2, -1, -1):
        gains[i] = -max(-gains[i], gains[i + 1])
    return gains[0]


def _square_is_safe_for(board: chess.Board, sq: int, side: chess.Color) -> bool:
    """
    True if the piece of ``side`` currently on ``sq`` is not losing material
    to a capturing sequence initiated by the opponent.
    """
    piece = board.piece_at(sq)
    if piece is None or piece.color != side:
        return True
    # Opponent initiates the capture with their least-valuable attacker.
    lva = _least_valuable_attacker(board, sq, not side)
    if lva is None:
        return True
    return _see(board, sq, lva) <= 0


def _gives_check_after(board_after: chess.Board) -> bool:
    return board_after.is_check()


# -------------------- Reason detectors --------------------

def _reason_gives_check(board_before: chess.Board, move: chess.Move, board_after: chess.Board) -> Optional[dict]:
    if board_before.gives_check(move):
        return {
            "code": "gives_check",
            "label": "Gives check",
            "detail": "The move puts the enemy king in check.",
        }
    return None


def _reason_mate_in_one(board_after: chess.Board) -> Optional[dict]:
    if board_after.is_checkmate():
        return {
            "code": "mate_in_one",
            "label": "Delivers checkmate",
            "detail": "The move ends the game: the enemy king has no escape.",
        }
    return None


def _reason_material(board_before: chess.Board, move: chess.Move) -> Tuple[Optional[dict], Optional[int]]:
    """Returns (reason_dict_or_None, see_value) for the candidate move."""
    is_cap = board_before.is_capture(move)
    if not is_cap:
        return None, None

    if board_before.is_en_passant(move):
        captured_val = PIECE_VALUES[chess.PAWN]
    else:
        cap_piece = board_before.piece_at(move.to_square)
        captured_val = PIECE_VALUES[cap_piece.piece_type] if cap_piece else 0

    see_val = _see(board_before, move.to_square, move.from_square)

    mover = board_before.piece_at(move.from_square)
    mover_val = PIECE_VALUES[mover.piece_type] if mover else 0

    if see_val >= 100:
        detail = f"Wins roughly {see_val} cp of material after the exchange."
        return (
            {"code": "wins_material", "label": "Wins material", "detail": detail},
            see_val,
        )
    # Equal trade: captured piece of similar value, SEE near zero.
    if abs(captured_val - mover_val) <= 30 and -30 <= see_val <= 50:
        return (
            {"code": "equal_trade", "label": "Equal trade", "detail": "Trades a piece for one of similar value."},
            see_val,
        )
    return None, see_val


def _reason_develops_minor(board_before: chess.Board, move: chess.Move) -> Optional[dict]:
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type not in (chess.KNIGHT, chess.BISHOP):
        return None
    back_rank = 0 if piece.color == chess.WHITE else 7
    if chess.square_rank(move.from_square) != back_rank:
        return None
    if chess.square_rank(move.to_square) == back_rank:
        return None
    name = "knight" if piece.piece_type == chess.KNIGHT else "bishop"
    return {
        "code": "develops_minor_piece",
        "label": f"Develops a {name}",
        "detail": f"Brings the {name} off the back rank into play.",
    }


def _reason_castles(board_before: chess.Board, move: chess.Move) -> Optional[dict]:
    if not board_before.is_castling(move):
        return None
    side = "kingside" if board_before.is_kingside_castling(move) else "queenside"
    return {
        "code": "castles_for_safety",
        "label": "Castles",
        "detail": f"Castles {side}, tucking the king to safety and connecting rooks.",
    }


def _attacked_center_squares(board: chess.Board, side: chess.Color) -> int:
    return sum(1 for sq in CENTER_SQUARES if board.is_attacked_by(side, sq))


def _reason_center(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> List[dict]:
    reasons: List[dict] = []
    if move.to_square in CENTER_SQUARES:
        piece = board_before.piece_at(move.from_square)
        name = PIECE_NAMES.get(piece.piece_type, "piece") if piece else "piece"
        reasons.append(
            {
                "code": "occupies_center",
                "label": "Occupies the center",
                "detail": f"Places a {name} on a central square.",
            }
        )
    before_ctrl = _attacked_center_squares(board_before, side)
    after_ctrl = _attacked_center_squares(board_after, side)
    if after_ctrl > before_ctrl and move.to_square not in CENTER_SQUARES:
        reasons.append(
            {
                "code": "claims_center",
                "label": "Claims the center",
                "detail": "Increases control of central squares.",
            }
        )
    return reasons


def _own_piece_squares(board: chess.Board, side: chess.Color, piece_type: int) -> List[int]:
    return list(board.pieces(piece_type, side))


def _piece_mobility(board: chess.Board, sq: int) -> int:
    """Pseudo-legal move count from sq for the current side to move.
    Returns 0 if the piece on sq is not owned by board.turn (no cheap way)."""
    piece = board.piece_at(sq)
    if piece is None:
        return 0
    # Temporarily flip side-to-move if needed, counting pseudo-legal only.
    b = board.copy(stack=False)
    if b.turn != piece.color:
        b.turn = piece.color
    return sum(1 for m in b.pseudo_legal_moves if m.from_square == sq)


def _reasons_opens_lines(
    board_before: chess.Board,
    board_after: chess.Board,
    side: chess.Color,
) -> List[dict]:
    """
    Detect that friendly bishop/rook/queen gained reach due to the move
    (e.g. a pawn moved out of the way). We compare per-piece mobility before
    and after, by piece type, ignoring the piece that actually moved.
    """
    out: List[dict] = []
    type_to_code = {
        chess.BISHOP: ("opens_bishop", "bishop"),
        chess.ROOK: ("opens_rook", "rook"),
        chess.QUEEN: ("opens_queen", "queen"),
    }
    for pt, (code, name) in type_to_code.items():
        before_sqs = set(_own_piece_squares(board_before, side, pt))
        after_sqs = set(_own_piece_squares(board_after, side, pt))
        common = before_sqs & after_sqs
        gained = 0
        for sq in common:
            gained += max(0, _piece_mobility(board_after, sq) - _piece_mobility(board_before, sq))
        if gained >= 2:
            out.append(
                {
                    "code": code,
                    "label": f"Opens a {name}",
                    "detail": f"Frees a diagonal/file for the {name}.",
                }
            )
    return out


def _fork_victims_from_square(
    board: chess.Board,
    attacker_sq: int,
    side: chess.Color,
) -> List[Tuple[int, chess.Piece]]:
    """
    Return opponent pieces on attacked squares that would be "won" by a fork,
    i.e. each victim is at least one of:
      - the enemy king (must move, so other victims can be taken),
      - a piece strictly higher in value than the attacker (trade wins material),
      - a piece the attacker can capture for free (no opposing defender).
    The attacker is assumed to live on ``attacker_sq`` and belong to ``side``.
    """
    attacker = board.piece_at(attacker_sq)
    if attacker is None or attacker.color != side:
        return []
    mover_val = PIECE_VALUES[attacker.piece_type]
    victims: List[Tuple[int, chess.Piece]] = []
    for tgt in board.attacks(attacker_sq):
        v = board.piece_at(tgt)
        if v is None or v.color == side:
            continue
        if v.piece_type == chess.KING:
            victims.append((tgt, v))
            continue
        v_val = PIECE_VALUES[v.piece_type]
        if v_val > mover_val + 30:
            victims.append((tgt, v))
            continue
        # Hanging victim (no defender) → attacker can take for free.
        defenders = board.attackers(not side, tgt)
        if not defenders:
            victims.append((tgt, v))
    return victims


def _reason_fork(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    """
    Positive fork for us: after the move, the moved piece attacks two or more
    opposing pieces that are either the king, higher-value, or undefended.

    We also require the fork to be "new" — if the same forking pattern already
    existed before the move, it's not the reason this move is good.
    """
    after_victims = _fork_victims_from_square(board_after, move.to_square, side)
    if len(after_victims) < 2:
        return None

    # Suppress if the attacker on from_square already forked the same or more
    # pieces before the move (i.e. the fork pre-existed).
    before_victims = _fork_victims_from_square(board_before, move.from_square, side)
    if len(before_victims) >= len(after_victims):
        return None

    moved = board_after.piece_at(move.to_square)
    attacker_name = PIECE_NAMES[moved.piece_type] if moved else "piece"
    victim_names = [PIECE_NAMES[v.piece_type] for _, v in after_victims[:3]]
    # Royal fork (king + queen) gets a sharper label.
    victim_types = {v.piece_type for _, v in after_victims}
    if chess.KING in victim_types and chess.QUEEN in victim_types:
        label = "Creates a royal fork"
    else:
        label = "Creates a fork"
    detail = (
        f"The {attacker_name} attacks the "
        + " and ".join(victim_names[:2])
        + " at once."
    )
    return {"code": "creates_fork", "label": label, "detail": detail}


def _reason_attacks_higher_value(
    board_after: chess.Board,
    move: chess.Move,
    side: chess.Color,
) -> Optional[dict]:
    moved = board_after.piece_at(move.to_square)
    if moved is None:
        return None
    mover_val = PIECE_VALUES[moved.piece_type]
    for tgt in board_after.attacks(move.to_square):
        victim = board_after.piece_at(tgt)
        if victim is None or victim.color == side:
            continue
        if PIECE_VALUES[victim.piece_type] > mover_val + 30:
            vname = PIECE_NAMES[victim.piece_type]
            mname = PIECE_NAMES[moved.piece_type]
            return {
                "code": "attacks_higher_value_piece",
                "label": f"Attacks the {vname}",
                "detail": f"The {mname} now attacks a more valuable {vname}.",
            }
    return None


def _find_hanging(board: chess.Board, side: chess.Color) -> List[int]:
    """Return squares where ``side`` has a piece that is hanging (SEE > 0 for opponent)."""
    out: List[int] = []
    for sq, piece in board.piece_map().items():
        if piece.color != side or piece.piece_type == chess.KING:
            continue
        lva = _least_valuable_attacker(board, sq, not side)
        if lva is None:
            continue
        if _see(board, sq, lva) > 0:
            out.append(sq)
    return out


def _reason_defends_hanging(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    hanging_before = set(_find_hanging(board_before, side))
    if not hanging_before:
        return None
    # Did the moved piece itself escape?
    if move.from_square in hanging_before and move.to_square not in _find_hanging(board_after, side):
        piece = board_before.piece_at(move.from_square)
        name = PIECE_NAMES[piece.piece_type] if piece else "piece"
        return {
            "code": "defends_hanging_piece",
            "label": "Saves a hanging piece",
            "detail": f"Moves the attacked {name} to safety.",
        }
    hanging_after = set(_find_hanging(board_after, side))
    saved = hanging_before - hanging_after - {move.from_square}
    if saved:
        return {
            "code": "defends_hanging_piece",
            "label": "Defends a hanging piece",
            "detail": "A friendly piece that was hanging is now defended.",
        }
    return None


def _is_passed_pawn(board: chess.Board, sq: int, color: chess.Color) -> bool:
    file = chess.square_file(sq)
    rank = chess.square_rank(sq)
    enemy = not color
    if color == chess.WHITE:
        ranks_ahead = range(rank + 1, 8)
    else:
        ranks_ahead = range(0, rank)
    for f in (file - 1, file, file + 1):
        if f < 0 or f > 7:
            continue
        for r in ranks_ahead:
            p = board.piece_at(chess.square(f, r))
            if p is not None and p.piece_type == chess.PAWN and p.color == enemy:
                return False
    return True


def _reason_passed_pawn(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type != chess.PAWN:
        return None
    was_passed = _is_passed_pawn(board_before, move.from_square, side)
    is_passed = _is_passed_pawn(board_after, move.to_square, side)
    if is_passed and not was_passed:
        return {
            "code": "creates_passed_pawn",
            "label": "Creates a passed pawn",
            "detail": "No enemy pawns can stop this pawn on its way to promotion.",
        }
    if is_passed and was_passed:
        return {
            "code": "advances_passed_pawn",
            "label": "Advances a passed pawn",
            "detail": "Pushes a passed pawn closer to promotion.",
        }
    return None


def _reason_piece_activity(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
) -> Optional[dict]:
    piece = board_before.piece_at(move.from_square)
    if piece is None:
        return None
    # Activity is only meaningful for non-pawn pieces.
    if piece.piece_type in (chess.PAWN, chess.KING):
        return None
    before_mob = _piece_mobility(board_before, move.from_square)
    after_mob = _piece_mobility(board_after, move.to_square)
    if after_mob - before_mob >= 3:
        name = PIECE_NAMES[piece.piece_type]
        return {
            "code": "improves_piece_activity",
            "label": f"Activates the {name}",
            "detail": f"The {name} has more squares to operate from its new post.",
        }
    return None


# -------------------- Warnings --------------------

def _warn_hangs_piece(
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    # Check the piece we just moved (on to_square).
    if not _square_is_safe_for(board_after, move.to_square, side):
        piece = board_after.piece_at(move.to_square)
        name = PIECE_NAMES[piece.piece_type] if piece else "piece"
        return {
            "code": "hangs_piece",
            "label": "Hangs a piece",
            "detail": f"The {name} can be won by the opponent with a capturing sequence.",
        }
    # Also scan other friendly pieces that were safe before but now hang.
    return None


def _king_shelter_squares(board: chess.Board, side: chess.Color) -> List[int]:
    king_sq = board.king(side)
    if king_sq is None:
        return []
    shelter: List[int] = []
    kf = chess.square_file(king_sq)
    kr = chess.square_rank(king_sq)
    pawn_rank = kr + (1 if side == chess.WHITE else -1)
    if 0 <= pawn_rank <= 7:
        for df in (-1, 0, 1):
            f = kf + df
            if 0 <= f <= 7:
                shelter.append(chess.square(f, pawn_rank))
    return shelter


def _warn_weakens_king_safety(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    # Flag: we moved a shelter pawn in front of our own (likely castled) king.
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type != chess.PAWN:
        return None
    if board_before.is_castling(move):
        return None
    shelter = set(_king_shelter_squares(board_before, side))
    if move.from_square not in shelter:
        return None
    # Only warn when the king appears "castled" (on g/c file or already moved).
    king_sq = board_before.king(side)
    if king_sq is None:
        return None
    king_file = chess.square_file(king_sq)
    if king_file not in (1, 2, 6, 5):  # b,c,f,g files — typical post-castling
        return None
    return {
        "code": "weakens_king_safety",
        "label": "Weakens king safety",
        "detail": "Pushes a pawn in front of the king, loosening its shelter.",
    }


def _warn_blocks_own_piece(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    """
    If a friendly slider (B/R/Q) loses >= 2 squares of mobility because our
    move parked a piece in its ray, flag the move as blocking.
    """
    loss_total = 0
    piece_type_losing: Optional[int] = None
    for pt in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        before_sqs = set(board_before.pieces(pt, side))
        after_sqs = set(board_after.pieces(pt, side))
        common = before_sqs & after_sqs
        for sq in common:
            delta = _piece_mobility(board_before, sq) - _piece_mobility(board_after, sq)
            if delta > 0:
                loss_total += delta
                piece_type_losing = pt
    if loss_total >= 2 and piece_type_losing is not None:
        name = PIECE_NAMES[piece_type_losing]
        return {
            "code": "blocks_own_piece",
            "label": "Blocks own piece",
            "detail": f"The destination square cuts across a friendly {name}'s line.",
        }
    return None


def _warn_allows_fork(
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    """
    After our move it is opponent's turn. If any opponent move lands a piece
    that immediately forks two or more of our pieces (king / higher-value /
    undefended), warn that we allowed a tactic.
    """
    opp = not side
    # board_after.turn == opp already; we iterate its legal moves.
    for m in board_after.legal_moves:
        b2 = board_after.copy()
        b2.push(m)
        victims = _fork_victims_from_square(b2, m.to_square, opp)
        if len(victims) >= 2:
            # Require the forking piece to be safe on its square; otherwise
            # it's just a blunder by the opponent, not a real threat.
            defender_sq = _least_valuable_attacker(b2, m.to_square, side)
            if defender_sq is None or _see(b2, m.to_square, defender_sq) <= 0:
                return {
                    "code": "allows_fork",
                    "label": "Allows a fork",
                    "detail": "The opponent's next move can fork two pieces.",
                }
    return None


def _warn_loses_tempo(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> Optional[dict]:
    """
    Classic "piece kicked by a pawn" tempo loss: the piece we just moved
    can be attacked by an enemy pawn on its next move, and moving away
    would be the sensible response.
    """
    piece = board_after.piece_at(move.to_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return None

    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    enemy = not side
    # Pawn attackers of to_square come from squares diagonally-forward from the
    # attacker's point of view. A pawn on (f±1, r') that can push or capture
    # onto (f, r±1) attacks us. We check if any enemy pawn currently 1-2 steps
    # behind those attacking squares could step forward to attack.
    step = 1 if enemy == chess.WHITE else -1
    attack_rank = to_rank - step  # enemy pawn attacks from one rank "behind" us
    if not (0 <= attack_rank <= 7):
        return None
    start_rank = 1 if enemy == chess.WHITE else 6
    for df in (-1, 1):
        f = to_file + df
        if f < 0 or f > 7:
            continue
        # Enemy pawn one square behind attack_rank can single-push there;
        # or an enemy pawn on its home rank can double-push to attack_rank.
        candidates: List[int] = []
        one_behind = attack_rank - step
        if 0 <= one_behind <= 7:
            candidates.append(one_behind)
        if start_rank + 2 * step == attack_rank:
            candidates.append(start_rank)
        for r in candidates:
            sq = chess.square(f, r)
            p = board_after.piece_at(sq)
            if p is None or p.piece_type != chess.PAWN or p.color != enemy:
                continue
            push_sq = chess.square(f, attack_rank)
            if board_after.piece_at(push_sq) is not None:
                continue
            if r == start_rank and r != one_behind:
                # Double push: the intermediate square must also be empty.
                mid_sq = chess.square(f, start_rank + step)
                if board_after.piece_at(mid_sq) is not None:
                    continue
            mname = PIECE_NAMES[piece.piece_type]
            return {
                "code": "loses_tempo_if_obvious",
                "label": "Likely kicked by a pawn",
                "detail": f"The {mname} can be chased away by a pawn advance.",
            }
    return None


# -------------------- Reason collection --------------------

# Codes that indicate the candidate's strength is "tactical" — these are the
# ones we generate "misses_*" comparison reasons for when the candidate is
# worse than best.
_TACTICAL_CODES = {"mate_in_one", "wins_material", "creates_fork", "gives_check"}


def _collect_positive_reasons(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    side: chess.Color,
) -> List[dict]:
    """Run every positive-reason detector and return de-duplicated results."""
    out: List[dict] = []

    r = _reason_mate_in_one(board_after)
    if r:
        out.append(r)
    r = _reason_gives_check(board_before, move, board_after)
    if r:
        out.append(r)
    r_mat, _see_val = _reason_material(board_before, move)
    if r_mat:
        out.append(r_mat)
    r = _reason_fork(board_before, move, board_after, side)
    if r:
        out.append(r)
    r = _reason_defends_hanging(board_before, move, board_after, side)
    if r:
        out.append(r)
    r = _reason_castles(board_before, move)
    if r:
        out.append(r)
    r = _reason_develops_minor(board_before, move)
    if r:
        out.append(r)
    out.extend(_reason_center(board_before, move, board_after, side))
    out.extend(_reasons_opens_lines(board_before, board_after, side))
    r = _reason_attacks_higher_value(board_after, move, side)
    if r:
        out.append(r)
    r = _reason_passed_pawn(board_before, move, board_after, side)
    if r:
        out.append(r)
    r = _reason_piece_activity(board_before, move, board_after)
    if r:
        out.append(r)

    seen: set = set()
    dedup: List[dict] = []
    for rr in out:
        c = rr["code"]
        if c in seen:
            continue
        seen.add(c)
        dedup.append(rr)

    # A fork already implies "attacks higher-value piece" — drop the weaker
    # label so the bullet list does not repeat the same idea twice.
    codes = {r["code"] for r in dedup}
    if "creates_fork" in codes and "attacks_higher_value_piece" in codes:
        dedup = [r for r in dedup if r["code"] != "attacks_higher_value_piece"]
    return dedup


_MISS_TEMPLATES: Dict[str, Tuple[str, str, str]] = {
    # best-move code -> (miss code, label, detail template with {best_san})
    "mate_in_one": (
        "misses_mate_in_one",
        "Misses mate in one",
        "{best_san} would have delivered checkmate.",
    ),
    "wins_material": (
        "misses_winning_capture",
        "Misses a winning capture",
        "{best_san} would have won material.",
    ),
    "creates_fork": (
        "misses_fork",
        "Misses a fork",
        "{best_san} would have forked two pieces.",
    ),
    "gives_check": (
        "misses_check",
        "Misses a strong check",
        "{best_san} would have given check.",
    ),
}

# Higher = more worth reporting as the primary "why it's bad". Mate beats
# material, which beats a fork, which beats a mere check (a check with no
# follow-up is usually already covered by a stronger theme).
_MISS_PRIORITY: Dict[str, int] = {
    "mate_in_one": 40,
    "wins_material": 30,
    "creates_fork": 20,
    "gives_check": 5,
}


def _missed_reasons(
    board: chess.Board,
    best_move: Optional[chess.Move],
    cand_codes: set,
    cand_san: str,
    side: chess.Color,
) -> List[dict]:
    """
    Compare the candidate to the engine's best move and surface "misses X"
    reasons for tactical themes the candidate passed up.

    Themes are ranked by tactical significance so the primary summary line
    references the most impactful missed idea (mate > material > fork > check).
    """
    if best_move is None:
        return []
    try:
        best_san = board.san(best_move)
    except Exception:
        best_san = best_move.uci()

    board_after_best = board.copy()
    board_after_best.push(best_move)
    best_reasons = _collect_positive_reasons(board, best_move, board_after_best, side)

    # Pick best-move tactical codes that candidate lacks, sorted by priority.
    missed_codes = [
        br["code"]
        for br in best_reasons
        if br["code"] in _MISS_TEMPLATES and br["code"] not in cand_codes
    ]
    missed_codes.sort(key=lambda c: _MISS_PRIORITY.get(c, 0), reverse=True)

    # Drop "misses_check" unless it's the only signal — a missed check without
    # a bigger theme is rarely the real reason the move is bad.
    stronger = [c for c in missed_codes if c != "gives_check"]
    if stronger:
        missed_codes = stronger

    out: List[dict] = []
    seen: set = set()
    for code in missed_codes:
        tpl = _MISS_TEMPLATES[code]
        mcode, label, detail_tpl = tpl
        if mcode in seen:
            continue
        seen.add(mcode)
        out.append({"code": mcode, "label": label, "detail": detail_tpl.format(best_san=best_san)})
    return out


# -------------------- Summaries --------------------

_QUALITY_LEAD = {
    "best": "is the engine's top choice",
    "excellent": "is nearly the top choice",
    "good": "is a solid move",
    "playable": "is playable",
    "inaccuracy": "is inaccurate",
    "mistake": "is a mistake",
    "blunder": "is a serious blunder",
}

_BAD_QUALITIES = frozenset({"inaccuracy", "mistake", "blunder"})


def _summarize(
    san: str,
    quality: str,
    positive_reasons: List[dict],
    missed_reasons: List[dict],
    warnings: List[dict],
) -> str:
    lead = _QUALITY_LEAD.get(quality, "is a move")

    if quality in _BAD_QUALITIES:
        # Prefer concrete "misses ..." over generic warnings when both exist.
        if missed_reasons:
            return f"{san} {lead}: {missed_reasons[0]['label'].lower()}."
        if warnings:
            return f"{san} {lead}: it {warnings[0]['label'].lower()}."
        # No specific negative found — stay honest, avoid listing positives
        # that could sound like endorsements of a bad move.
        return f"{san} {lead} here."

    positives = [r["label"].lower() for r in positive_reasons[:2]]
    if positives:
        parts = " and ".join(positives)
        return f"{san} {lead} because it {parts}."
    if warnings:
        return f"{san} {lead}, but {warnings[0]['label'].lower()}."
    return f"{san} {lead}."


# -------------------- Orchestrator --------------------

def _get_root_analysis(fen: str, root_sims: int, top_k: int = 12) -> Dict[str, Any]:
    return svc.analyze_fen(fen, max(1, root_sims), max(1, top_k))


def _find_candidate_info(root_moves: List[Dict[str, Any]], uci: str) -> Optional[Dict[str, Any]]:
    u = uci.lower()
    for m in root_moves:
        if str(m.get("uci", "")).lower() == u:
            return m
    return None


def explain_move(
    fen: str,
    uci: str,
    root_sims: int = 64,
    pv_plies: int = 6,
) -> Dict[str, Any]:
    """
    Rule-based explanation for a candidate move.

    Returns a dict shaped for ``POST /api/explain_move``. See the module
    docstring for the perspective contract.
    """
    uci = (uci or "").strip().lower()
    if not uci:
        return {"error": "uci required"}

    try:
        nf = svc.normalize_fen(fen)
    except Exception as e:
        return {"error": f"bad fen: {e}"}

    board = chess.Board(nf)

    # Terminal root — nothing to explain.
    if board.is_game_over(claim_draw=True):
        return {
            "error": "terminal position",
            "fen": nf,
            "uci": uci,
        }

    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return {"error": "bad uci", "fen": nf, "uci": uci}

    if move not in board.legal_moves:
        return {"error": "illegal move", "fen": nf, "uci": uci}

    san = board.san(move)

    root = _get_root_analysis(nf, root_sims=root_sims, top_k=12)
    root_moves: List[Dict[str, Any]] = root.get("moves") or []
    best_info = root_moves[0] if root_moves else None

    cand_info = _find_candidate_info(root_moves, uci)
    # Root ("best") cp/mate — in root STM perspective.
    best_cp = best_info.get("cp") if best_info else None
    best_mate = best_info.get("mate") if best_info else None
    best_cp_norm = _score_to_cp(best_cp, best_mate)

    if cand_info is not None:
        cand_cp = cand_info.get("cp")
        cand_mate = cand_info.get("mate")
    else:
        # Candidate wasn't in top_k; run a tiny secondary analysis on the
        # resulting position, then flip sign for root STM.
        cand_cp = None
        cand_mate = None
        try:
            board_after = board.copy()
            board_after.push(move)
            limit = svc.analysis_limit_from_sims(max(1, root_sims))

            def _go(eng):
                svc.configure_analysis_strength(eng)
                return eng.analyse(board_after, limit)

            info = svc.with_engine(_go)
            rel = info["score"].relative
            # rel is from opponent's perspective at board_after; flip for root STM.
            if rel.is_mate():
                m = rel.mate()
                cand_mate = -m if m is not None else None
            else:
                cp = rel.score()
                cand_cp = -cp if cp is not None else 0
        except Exception:
            pass

    cand_cp_norm = _score_to_cp(cand_cp, cand_mate)
    cp_loss = max(0, best_cp_norm - cand_cp_norm)
    quality = _classify_quality(cp_loss, best_mate, cand_mate)

    # Build post-move board for feature extraction.
    board_after = board.copy()
    board_after.push(move)
    side = board.turn

    positive_reasons = _collect_positive_reasons(board, move, board_after, side)

    # Resolve the best move to compare against (for "misses_*" reasons).
    best_move_obj: Optional[chess.Move] = None
    if best_info is not None:
        try:
            bm = chess.Move.from_uci(str(best_info.get("uci", "")).lower())
            if bm in board.legal_moves and bm != move:
                best_move_obj = bm
        except ValueError:
            best_move_obj = None

    cand_codes = {r["code"] for r in positive_reasons}
    missed = _missed_reasons(board, best_move_obj, cand_codes, san, side)

    # Warnings
    warnings: List[dict] = []
    w = _warn_hangs_piece(move, board_after, side)
    if w:
        warnings.append(w)
    w = _warn_weakens_king_safety(board, move, board_after, side)
    if w:
        warnings.append(w)
    w = _warn_blocks_own_piece(board, move, board_after, side)
    if w:
        warnings.append(w)
    w = _warn_loses_tempo(board, move, board_after, side)
    if w:
        warnings.append(w)
    w = _warn_allows_fork(board_after, side)
    if w:
        warnings.append(w)

    # Final reason ordering for the UI:
    #   - Bad-quality moves: lead with concrete "misses_*" items so the list
    #     explains *why* it's bad, then show any remaining positives.
    #   - Good-quality moves: positives first; missed_reasons should be rare
    #     here but still tail the list if present.
    if quality in _BAD_QUALITIES:
        reasons_final = missed + positive_reasons
    else:
        reasons_final = positive_reasons + missed

    # De-dup and cap (2–5 bullets per spec).
    seen_codes: set = set()
    dedup: List[dict] = []
    for rr in reasons_final:
        c = rr["code"]
        if c in seen_codes:
            continue
        seen_codes.add(c)
        dedup.append(rr)
    reasons = dedup[:5]

    # Build PV via existing pipeline; mirrors /api/pv but is called directly.
    pv_ucis: List[str] = []
    pv_sans: List[str] = []
    try:
        pv_payload = svc.pv_line(
            nf,
            uci,
            max(1, int(pv_plies)),
            max(1, int(root_sims)),
            max(1, int(round(root_sims * 0.35)) or 12),
        )
        pv_ucis = list(pv_payload.get("line") or [])
    except Exception:
        pv_ucis = []

    # Convert PV to SAN at the root for the client.
    if pv_ucis:
        b = board.copy()
        for u in pv_ucis:
            try:
                m = chess.Move.from_uci(u)
            except ValueError:
                break
            if m not in b.legal_moves:
                break
            pv_sans.append(b.san(m))
            b.push(m)

    summary = _summarize(san, quality, positive_reasons, missed, warnings)

    return {
        "fen": nf,
        "uci": uci,
        "san": san,
        "quality": quality,
        "cp_before": int(best_cp_norm) if best_info is not None else None,
        "cp_after_move": int(cand_cp_norm) if (cand_info is not None or cand_cp is not None or cand_mate is not None) else None,
        "cp_loss_vs_best": int(cp_loss),
        "summary": summary,
        "reasons": reasons,
        "warnings": warnings,
        "pv": pv_sans or pv_ucis,
    }
