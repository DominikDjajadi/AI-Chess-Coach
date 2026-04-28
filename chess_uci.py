"""UCI helpers and simple mate detection for the custom ``Game`` engine (no ML deps)."""
from __future__ import annotations

from game import Game
from objects import Color, Move, Outcome, PieceType, Position


def pos_to_uci(p: Position) -> str:
    return f"{chr(ord('a') + p.x)}{p.y + 1}"


def move_to_uci(m: Move) -> str:
    s = pos_to_uci(m.start)
    e = pos_to_uci(m.end)
    if m.promo is None:
        return s + e
    promo_map = {
        PieceType.QUEEN: "q",
        PieceType.ROOK: "r",
        PieceType.BISHOP: "b",
        PieceType.KNIGHT: "n",
    }
    return s + e + promo_map.get(m.promo, "q")


def winner_for_color(mover: Color) -> Outcome:
    return Outcome.WHITE_WIN if mover == Color.WHITE else Outcome.BLACK_WIN


def find_mate_in_one(game: Game):
    mover = game.turn
    mates = []
    for mv in game.legal_moves():
        st = game.push(mv)
        terminal, res = game.is_terminal()
        game.pop(st)
        if terminal and res == winner_for_color(mover):
            mates.append(mv)
    return mates
