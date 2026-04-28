"""
Tests for webapp.move_explainer.

Focus on rule-based board-feature detection and the cp-loss quality bucket.
Stockfish is stubbed via monkeypatching so these run without an engine.
"""
from __future__ import annotations

import chess
import pytest

from webapp import move_explainer as mx


# ------------------ Pure helpers ------------------

def test_quality_buckets():
    assert mx._classify_quality(0, None, None) == "best"
    assert mx._classify_quality(10, None, None) == "excellent"
    assert mx._classify_quality(40, None, None) == "good"
    assert mx._classify_quality(90, None, None) == "playable"
    assert mx._classify_quality(180, None, None) == "inaccuracy"
    assert mx._classify_quality(350, None, None) == "mistake"
    assert mx._classify_quality(900, None, None) == "blunder"


def test_quality_mate_aware_blunder():
    # Root had winning mate, candidate throws it away with big cp swing.
    q = mx._classify_quality(cp_loss=900, mate_before=3, mate_after=None)
    assert q == "blunder"
    q = mx._classify_quality(cp_loss=150, mate_before=3, mate_after=None)
    assert q == "mistake"


def test_score_to_cp_mate_ordering():
    # Winning mate-in-1 is better than mate-in-5.
    assert mx._score_to_cp(None, 1) > mx._score_to_cp(None, 5)
    # Losing mates are negative.
    assert mx._score_to_cp(None, -3) < 0


# ------------------ Rule-based feature detectors ------------------

def _board_after(fen: str, uci: str):
    b = chess.Board(fen)
    m = chess.Move.from_uci(uci)
    assert m in b.legal_moves, f"{uci} not legal in {fen}"
    a = b.copy()
    a.push(m)
    return b, m, a


def test_gives_check_detected():
    # White queen delivers check along h5-e8 via Qh5 after 1.e4 e5 2.Bc4 Nc6 ...
    # Easier: simple forced check position.
    fen = "rnbqkbnr/ppp2ppp/8/3pp3/8/4P3/PPPP1PPP/RNBQKBNR w KQkq - 0 3"
    # White plays Qh5+ from d1? Queen is on d1, not reaching h5 in one. Let's pick a clear check.
    # Construct: black king e8, white queen on e1, empty e-file.
    fen = "4k3/8/8/8/8/8/8/4Q1K1 w - - 0 1"
    b, m, a = _board_after(fen, "e1e7")
    r = mx._reason_gives_check(b, m, a)
    assert r is not None
    assert r["code"] == "gives_check"


def test_mate_in_one_detected():
    # Back-rank mate: black king h8, pawns g7/h7, white rook a1 -> Ra8#
    fen = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
    b, m, a = _board_after(fen, "a1a8")
    r = mx._reason_mate_in_one(a)
    assert r is not None
    assert r["code"] == "mate_in_one"


def test_claims_center_from_startpos_e4():
    fen = chess.STARTING_FEN
    b, m, a = _board_after(fen, "e2e4")
    reasons = mx._reason_center(b, m, a, side=chess.WHITE)
    codes = {r["code"] for r in reasons}
    assert "occupies_center" in codes


def test_develops_minor_piece_nf3():
    fen = chess.STARTING_FEN
    b, m, a = _board_after(fen, "g1f3")
    r = mx._reason_develops_minor(b, m)
    assert r is not None
    assert r["code"] == "develops_minor_piece"


def test_castles_for_safety_kingside():
    # Typical position right before short castling.
    fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 4 5"
    b, m, a = _board_after(fen, "e1g1")
    r = mx._reason_castles(b, m)
    assert r is not None
    assert r["code"] == "castles_for_safety"
    assert "kingside" in r["detail"].lower()


def test_defends_hanging_piece_saves_attacked_bishop():
    # White bishop on c4 hangs to a black pawn on b5. Moving it should be a save.
    fen = "rnbqkbnr/p1pppppp/8/1p6/2B5/8/PPPP1PPP/RNBQK1NR w KQkq - 0 1"
    # Ensure the bishop is truly hanging before the move.
    board = chess.Board(fen)
    hanging = mx._find_hanging(board, chess.WHITE)
    assert chess.C4 in hanging
    # Retreat Bb3 — still safe and no longer hanging.
    b, m, a = _board_after(fen, "c4b3")
    r = mx._reason_defends_hanging(b, m, a, chess.WHITE)
    assert r is not None
    assert r["code"] == "defends_hanging_piece"


def test_hangs_piece_warning_when_moving_into_attack():
    # White bishop moves from c1 to h6 where a pawn on g7 attacks it.
    fen = "rnbqkbnr/pppppppp/8/8/8/7P/PPPPPPP1/RNBQKBNR w KQkq - 0 1"
    # Actually need a real attacker. Use: black pawn on g7 attacks h6.
    # Startpos has g7 pawn. Push Bh6? Bishop on c1 can't reach h6 in one without obstructions.
    # Construct a minimal position.
    fen = "4k3/6p1/8/8/8/2B5/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "c3h8")
    # Not actually attacked there. Try instead:
    fen = "4k3/6p1/8/8/3B4/8/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "d4h8")
    # Bh8 isn't attacked either. Use a classic: Bh6 vs pawn on g7.
    fen = "4k3/6p1/8/8/8/8/3B4/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "d2h6")
    w = mx._warn_hangs_piece(m, a, chess.WHITE)
    assert w is not None
    assert w["code"] == "hangs_piece"


def test_wins_material_basic_capture():
    # White rook captures a free black pawn.
    fen = "4k3/8/8/8/8/8/3p4/3RK3 w - - 0 1"
    b, m, a = _board_after(fen, "d1d2")
    r, see = mx._reason_material(b, m)
    assert r is not None
    assert r["code"] == "wins_material"
    assert see and see >= 100


def test_equal_trade_pawn_for_pawn():
    # Black pawn on c5 is defended by d6 pawn; dxc5 dxc5 is a clean trade.
    fen = "4k3/8/3p4/2p5/3P4/8/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "d4c5")
    r, see = mx._reason_material(b, m)
    assert r is not None, f"no material reason; see={see}"
    assert r["code"] == "equal_trade", f"got {r['code']}, see={see}"


def test_fork_detected_knight_forks_king_and_rook():
    # White knight jumps e5 -> f7, attacking black king h8 and rook d8.
    fen = "3r3k/8/8/4N3/8/8/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "e5f7")
    r = mx._reason_fork(b, m, a, chess.WHITE)
    assert r is not None, "expected fork reason"
    assert r["code"] == "creates_fork"


def test_fork_not_credited_to_unrelated_move():
    # Knight already forks (from f7) but the move made is a king step —
    # a king move must not be credited with creating a fork.
    fen = "3r3k/5N2/8/8/8/8/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "e1e2")
    r = mx._reason_fork(b, m, a, chess.WHITE)
    assert r is None


def test_allows_fork_warning_when_opponent_can_knight_fork():
    # White plays a harmless pawn push; black replies with Nd4-c2 forking
    # white king on e1 and rook on a1.
    fen = "4k3/8/8/8/3n4/8/P7/R3K3 w Q - 0 1"
    b = chess.Board(fen)
    move = chess.Move.from_uci("a2a3")
    assert move in b.legal_moves
    a = b.copy()
    a.push(move)
    w = mx._warn_allows_fork(a, chess.WHITE)
    assert w is not None, "expected allows_fork warning"
    assert w["code"] == "allows_fork"


def test_passed_pawn_creation():
    # White pawn on e5 with no black pawns on d/e/f ahead becomes passed after e5-e6.
    fen = "4k3/8/8/4P3/8/8/8/4K3 w - - 0 1"
    b, m, a = _board_after(fen, "e5e6")
    # Before e5 pawn: already passed (no black pawns anywhere). So this hits advances_passed_pawn.
    r = mx._reason_passed_pawn(b, m, a, chess.WHITE)
    assert r is not None
    assert r["code"] in ("creates_passed_pawn", "advances_passed_pawn")


# ------------------ End-to-end via explain_move with stubbed engine ------------------

class _FakeAnalyze:
    """Stubs svc.analyze_fen / svc.pv_line so explain_move needs no Stockfish."""

    def __init__(self, best_uci: str, best_cp: int = 50, cand_cp_map=None):
        self.best_uci = best_uci
        self.best_cp = best_cp
        self.cand_cp_map = cand_cp_map or {}

    def analyze_fen(self, fen, sims, top_k):
        from webapp import chess_service as svc

        nf = svc.normalize_fen(fen)
        board = chess.Board(nf)
        legal = [m.uci() for m in board.legal_moves]
        # Ensure best is first; include a few others with lower cp.
        moves = []
        if self.best_uci in legal:
            moves.append(
                {"uci": self.best_uci, "rank": 1, "q": 0.5, "cp": self.best_cp, "mate": None}
            )
        for u in legal:
            if u == self.best_uci:
                continue
            cp = self.cand_cp_map.get(u, self.best_cp - 80)
            moves.append({"uci": u, "rank": len(moves) + 1, "q": 0.3, "cp": cp, "mate": None})
        return {
            "terminal": False,
            "fen": nf,
            "root_value": 0.5,
            "root_cp": self.best_cp,
            "root_mate": None,
            "moves": moves,
        }

    def pv_line(self, fen, uci, total_plies, root_sims, fallback_sims):
        return {"line": [uci], "evals": [0.0], "mode": "stockfish_pv"}


@pytest.fixture
def stubbed_engine(monkeypatch):
    from webapp import chess_service as svc

    fake = _FakeAnalyze(best_uci="e2e4", best_cp=30)
    monkeypatch.setattr(svc, "analyze_fen", fake.analyze_fen)
    monkeypatch.setattr(svc, "pv_line", fake.pv_line)
    return fake


def test_explain_illegal_move_returns_error(stubbed_engine):
    out = mx.explain_move(chess.STARTING_FEN, "e2e5")
    assert out.get("error")


def test_explain_bad_uci_returns_error(stubbed_engine):
    out = mx.explain_move(chess.STARTING_FEN, "zzzz")
    assert out.get("error")


def test_explain_terminal_position_returns_error(stubbed_engine):
    # Fool's-mate-ish final position: white is checkmated.
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 3"
    out = mx.explain_move(fen, "e1e2")
    assert out.get("error") == "terminal position"


def test_explain_best_move_classified_best(stubbed_engine):
    out = mx.explain_move(chess.STARTING_FEN, "e2e4")
    assert "error" not in out
    assert out["quality"] == "best"
    assert out["cp_loss_vs_best"] == 0
    assert out["san"] == "e4"
    # Should surface a center/development reason.
    codes = {r["code"] for r in out["reasons"]}
    assert codes & {"occupies_center", "claims_center"}


def test_explain_non_best_move_quality_scales_with_cp_loss(monkeypatch):
    from webapp import chess_service as svc

    # Best = e4 at +30, candidate d2d3 at -100 → loss 130 → "inaccuracy".
    fake = _FakeAnalyze(
        best_uci="e2e4",
        best_cp=30,
        cand_cp_map={"d2d3": -100},
    )
    monkeypatch.setattr(svc, "analyze_fen", fake.analyze_fen)
    monkeypatch.setattr(svc, "pv_line", fake.pv_line)

    out = mx.explain_move(chess.STARTING_FEN, "d2d3")
    assert out["cp_loss_vs_best"] == 130
    assert out["quality"] == "inaccuracy"


def test_explain_surfaces_misses_fork(monkeypatch):
    """
    Position where the best move creates a knight fork, but the candidate
    move ignores it. The explanation should report 'misses_fork' and the
    summary should reference it (no cheerful positive instead).
    """
    from webapp import chess_service as svc

    # White: Ke1, Ne5. Black: Kh8, Rd8. Best = Nf7 (fork). Candidate = Ke2.
    fen = "3r3k/8/8/4N3/8/8/8/4K3 w - - 0 1"

    fake = _FakeAnalyze(
        best_uci="e5f7",
        best_cp=500,
        cand_cp_map={"e1e2": -50},
    )
    monkeypatch.setattr(svc, "analyze_fen", fake.analyze_fen)
    monkeypatch.setattr(svc, "pv_line", fake.pv_line)

    out = mx.explain_move(fen, "e1e2")
    assert "error" not in out
    codes = [r["code"] for r in out["reasons"]]
    assert "misses_fork" in codes, f"expected misses_fork; got {codes}"
    # Summary should name the miss, not a random positive feature.
    assert "fork" in out["summary"].lower()
    # Quality must be bad for misses_* to be promoted to the top.
    assert out["quality"] in ("inaccuracy", "mistake", "blunder")


def test_summary_for_blunder_does_not_endorse_move(monkeypatch):
    """Bad moves with no specific negative should NOT lead with positives."""
    from webapp import chess_service as svc

    # Best = e4 at +100, candidate g2g3 at -600 → loss 700 → "blunder".
    fake = _FakeAnalyze(
        best_uci="e2e4",
        best_cp=100,
        cand_cp_map={"g2g3": -600},
    )
    monkeypatch.setattr(svc, "analyze_fen", fake.analyze_fen)
    monkeypatch.setattr(svc, "pv_line", fake.pv_line)

    out = mx.explain_move(chess.STARTING_FEN, "g2g3")
    assert out["quality"] == "blunder"
    s = out["summary"].lower()
    # Must not describe the blunder as "because it ..." (an endorsement).
    assert "because it" not in s
