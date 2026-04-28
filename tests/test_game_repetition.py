# test_game_repetition_integrity.py
from game import Game
from objects import Position, PieceType, Outcome


def push_uci(g: Game, uci: str):
    sx = ord(uci[0]) - ord("a")
    sy = int(uci[1]) - 1
    ex = ord(uci[2]) - ord("a")
    ey = int(uci[3]) - 1

    promo = None
    if len(uci) == 5:
        promo = {
            "q": PieceType.QUEEN,
            "r": PieceType.ROOK,
            "b": PieceType.BISHOP,
            "n": PieceType.KNIGHT,
        }[uci[4].lower()]

    ok = g.make_move(Position(sx, sy), Position(ex, ey), promotion=promo)
    assert ok, f"move failed: {uci}"


def test_threefold_repetition_counts_reach_draw():
    g = Game()
    start_key = g.position_key()

    assert g.repetition_count_for_key(start_key) == 1

    # 1. Nf3 Nf6 2. Ng1 Ng8  -> back to start position
    push_uci(g, "g1f3")
    push_uci(g, "g8f6")
    push_uci(g, "f3g1")
    push_uci(g, "f6g8")

    assert g.position_key() == start_key
    assert g.repetition_count_for_key(start_key) == 2

    # Repeat once more -> third occurrence
    push_uci(g, "g1f3")
    push_uci(g, "g8f6")
    push_uci(g, "f3g1")
    push_uci(g, "f6g8")

    assert g.position_key() == start_key
    assert g.repetition_count_for_key(start_key) == 3

    terminal, result = g.is_terminal()
    assert terminal is True
    assert result == Outcome.DRAW


def test_would_repeat_count_is_side_effect_free():
    g = Game()
    start_key = g.position_key()
    start_rep = g.repetition_count_for_key(start_key)

    mv = g.legal_moves()[0]
    _ = g.would_repeat_count(mv.start, mv.end, mv.promo)

    # State and repetition bookkeeping should be unchanged after speculative lookahead.
    assert g.position_key() == start_key
    assert g.repetition_count_for_key(start_key) == start_rep