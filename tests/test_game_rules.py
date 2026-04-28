from game import Game
from objects import Color, Outcome

def test_startpos_has_20_legal_moves():
    g = Game()
    assert len(g.legal_moves()) == 20

def test_push_pop_roundtrip_preserves_position_key():
    g = Game()
    k0 = g.position_key()
    mv = g.legal_moves()[0]
    st = g.push(mv)
    g.pop(st)
    assert g.position_key() == k0

def test_fools_mate_checkmate_via_push_uci(push_uci):
    # 1.f3 e5 2.g4 Qh4#
    g = Game()
    push_uci(g, "f2f3")
    push_uci(g, "e7e5")
    push_uci(g, "g2g4")
    push_uci(g, "d8h4")

    terminal, res = g.is_terminal()
    assert terminal is True
    assert res in (Outcome.WHITE_WIN, Outcome.BLACK_WIN)

    # Side to move is checkmated: no legal moves and in check
    assert not g.has_any_legal_moves(g.turn)
    assert g.is_in_check(g.turn)

def test_stalemate_detection_from_fen():
    # "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"
    g = Game()
    g.set_fen("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")

    terminal, res = g.is_terminal()
    assert terminal is True
    assert res == Outcome.DRAW
    assert not g.has_any_legal_moves(g.turn)
    assert not g.is_in_check(g.turn)

def test_halfmove_clock_resets_on_pawn_move(push_uci):
    g = Game()

    # Make a quiet knight move to increment halfmove clock.
    push_uci(g, "g1f3")
    assert g.halfmove_clock == 1

    # Make a pawn move -> should reset to 0
    push_uci(g, "a7a6")  # black pawn
    assert g.halfmove_clock == 0
