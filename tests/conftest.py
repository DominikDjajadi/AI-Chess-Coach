import os
import random

import pytest


@pytest.fixture(autouse=True)
def _determinism():
    os.environ.setdefault("PYTHONHASHSEED", "0")
    random.seed(0)


def _uci_to_pos(uci_sq: str):
    file = ord(uci_sq[0]) - ord("a")
    rank = int(uci_sq[1]) - 1
    from objects import Position

    return Position(file, rank)


@pytest.fixture
def push_uci():
    def _push(game, uci: str):
        from objects import Move, PieceType

        start = _uci_to_pos(uci[0:2])
        end = _uci_to_pos(uci[2:4])
        promo = None
        if len(uci) == 5:
            ch = uci[4].lower()
            promo = {
                "q": PieceType.QUEEN,
                "r": PieceType.ROOK,
                "b": PieceType.BISHOP,
                "n": PieceType.KNIGHT,
            }[ch]
        return game.push(Move(start, end, promo))

    return _push
