from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Dict
from enum import Enum
from dataclasses import dataclass, field

ROW_SIZE = 8
COLUMN_SIZE = 8

class Outcome(Enum):
    ONGOING = 'ongoing'
    WHITE_WIN = 'white_win'
    BLACK_WIN = 'black_win'
    DRAW = 'draw'

class Color(Enum):
    WHITE = 'white'
    BLACK = 'black'

class PieceType(Enum):
    PAWN = 'pawn'
    ROOK = 'rook'
    KNIGHT = 'knight'
    BISHOP = 'bishop'
    QUEEN = 'queen'
    KING = 'king'


@dataclass
class Position:
    x: int
    y: int

@dataclass
class Piece(ABC):
    color: Color
    type: PieceType

    @abstractmethod
    def can_move_to(self, start: Position, end: Position, board) -> bool:
        pass

    @abstractmethod
    def can_capture(self, start: Position, end: Position, board) -> bool:
        pass

    def valid_move(self, start: Position, end: Position, board) -> bool:
        return self.can_move_to(start, end, board) or self.can_capture(start, end, board)
    
    def is_blocked(self, start: Position, end: Position, board) -> bool:
        step_x = 0 if start.x == end.x else (1 if end.x > start.x else -1)
        step_y = 0 if start.y == end.y else (1 if end.y > start.y else -1)
        current = Position(start.x + step_x, start.y + step_y)
        while current != end:
            if not board.is_empty(current):
                return True
            current = Position(current.x + step_x, current.y + step_y)
        return False

    def is_opponent(self, other: 'Piece') -> bool:
        return self.color != other.color
    
@dataclass
class Move:
    start: "Position"
    end: "Position"
    promo: Optional["PieceType"] = None  # None means no promotion (queen is handled in normal channels)

@dataclass
class MoveRecord:
    # what moved / where
    start: "Position"
    end: "Position"
    moved_piece: "Piece"

    # captures (including en passant)
    captured_piece: Optional["Piece"]
    ep_captured_at: Optional["Position"]  # square where EP pawn was actually removed

    # promotion (if any)
    was_promotion: bool
    promo_piece: Optional["Piece"]

    # castling rook move (if any)
    rook_start: Optional["Position"]
    rook_end: Optional["Position"]

    # board state snapshot to restore
    prev_en_passant: Optional["Position"]
    prev_castling_rights: Dict["Color", Dict[str, bool]]
    prev_halfmove_clock: int

class Pawn(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.PAWN)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        direction = 1 if self.color == Color.WHITE else -1
        if start.x == end.x and end.y - start.y == direction:
            return board.is_empty(end)
        if start.x == end.x and ((self.color == Color.WHITE and start.y == 1) or (self.color == Color.BLACK and start.y == 6)) and end.y - start.y == 2 * direction:
            return board.is_empty(end) and board.is_empty(Position(start.x, start.y + direction))
        return False

    def can_capture(self, start: Position, end: Position, board) -> bool:
        direction = 1 if self.color == Color.WHITE else -1
        if abs(start.x - end.x) == 1 and end.y - start.y == direction:
            target_piece = board.get_piece(end)
            return target_piece is not None and self.is_opponent(target_piece)
        return False
    
class Rook(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.ROOK)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        if start.x != end.x and start.y != end.y:
            return False
        if self.is_blocked(start, end, board):
            return False
        return board.is_empty(end)

    def can_capture(self, start: Position, end: Position, board) -> bool:
        target_piece = board.get_piece(end)
        if target_piece is None or not self.is_opponent(target_piece):
            return False
        if start.x != end.x and start.y != end.y:
            return False
        return not self.is_blocked(start, end, board)
    
class Knight(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.KNIGHT)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        dx = abs(start.x - end.x)
        dy = abs(start.y - end.y)
        if (dx == 2 and dy == 1) or (dx == 1 and dy == 2):
            return board.is_empty(end)
        return False

    def can_capture(self, start: Position, end: Position, board) -> bool:
        target_piece = board.get_piece(end)
        if target_piece is None or not self.is_opponent(target_piece):
            return False
        dx = abs(start.x - end.x)
        dy = abs(start.y - end.y)
        return (dx == 2 and dy == 1) or (dx == 1 and dy == 2)
    
class Bishop(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.BISHOP)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        if abs(start.x - end.x) != abs(start.y - end.y):
            return False
        if self.is_blocked(start, end, board):
            return False
        return board.is_empty(end)

    def can_capture(self, start: Position, end: Position, board) -> bool:
        target_piece = board.get_piece(end)
        if target_piece is None or not self.is_opponent(target_piece):
            return False
        if abs(start.x - end.x) != abs(start.y - end.y):
            return False
        return not self.is_blocked(start, end, board)
    
class Queen(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.QUEEN)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        if start.x != end.x and start.y != end.y and abs(start.x - end.x) != abs(start.y - end.y):
            return False
        if self.is_blocked(start, end, board):
            return False
        return board.is_empty(end)

    def can_capture(self, start: Position, end: Position, board) -> bool:
        target_piece = board.get_piece(end)
        if target_piece is None or not self.is_opponent(target_piece):
            return False
        if start.x != end.x and start.y != end.y and abs(start.x - end.x) != abs(start.y - end.y):
            return False
        return not self.is_blocked(start, end, board)
    
class King(Piece):
    def __init__(self, color: Color):
        super().__init__(color, PieceType.KING)

    def can_move_to(self, start: Position, end: Position, board) -> bool:
        if max(abs(start.x - end.x), abs(start.y - end.y)) != 1:
            return False
        return board.is_empty(end)

    def can_capture(self, start: Position, end: Position, board) -> bool:
        target_piece = board.get_piece(end)
        if target_piece is None or not self.is_opponent(target_piece):
            return False
        return max(abs(start.x - end.x), abs(start.y - end.y)) == 1
    

HOME = {
    Color.WHITE: {"king": Position(4, 0), "KR": Position(7, 0), "QR": Position(0, 0)},
    Color.BLACK: {"king": Position(4, 7), "KR": Position(7, 7), "QR": Position(0, 7)},
}


_KNIGHT = [(+1,+2),(+2,+1),(+2,-1),(+1,-2),(-1,-2),(-2,-1),(-2,+1),(-1,+2)]
_KING   = [(-1,-1),(-1,0),(-1,+1),(0,-1),(0,+1),(+1,-1),(+1,0),(+1,+1)]
_DIRS_B = [(-1,-1),(-1,+1),(+1,-1),(+1,+1)]
_DIRS_R = [(-1,0),(+1,0),(0,-1),(0,+1)]
_DIRS_Q = _DIRS_B + _DIRS_R

@dataclass
class Board:
    grid: List[List[Optional[Piece]]]
    en_passant_target: Optional[Position] = None
    castling_rights: dict = field(default_factory=lambda: {
        Color.WHITE: {'K': True, 'Q': True},
        Color.BLACK: {'K': True, 'Q': True},
    })

    def __init__(self):
        self.grid = [[None for _ in range(COLUMN_SIZE)] for _ in range(ROW_SIZE)]
        self.en_passant_target = None
        self.castling_rights = {
            Color.WHITE: {'K': True, 'Q': True},
            Color.BLACK: {'K': True, 'Q': True},
        }
        self.create_default_board()

    def _pseudo_pawn(self, start: Position, color: Color):
        sx, sy = start.x, start.y
        dirc = +1 if color == Color.WHITE else -1
        home = 1 if color == Color.WHITE else 6
        last = 7 if color == Color.WHITE else 0
        out = []

        # forward 1
        x, y = sx, sy + dirc
        if 0 <= y < ROW_SIZE and self.grid[y][x] is None:
            if y == last:
                for promo in (PieceType.QUEEN, PieceType.ROOK, PieceType.BISHOP, PieceType.KNIGHT):
                    out.append(Move(start, Position(x, y), promo))
            else:
                out.append(Move(start, Position(x, y), None))
            # forward 2 from home if clear
            if sy == home:
                y2 = sy + 2*dirc
                if 0 <= y2 < ROW_SIZE and self.grid[y2][x] is None:
                    out.append(Move(start, Position(x, y2), None))

        # captures
        for dx in (-1, +1):
            cx, cy = sx + dx, sy + dirc
            if 0 <= cx < COLUMN_SIZE and 0 <= cy < ROW_SIZE:
                tgt = self.grid[cy][cx]
                if tgt and tgt.color != color:
                    if cy == last:
                        for promo in (PieceType.QUEEN, PieceType.ROOK, PieceType.BISHOP, PieceType.KNIGHT):
                            out.append(Move(start, Position(cx, cy), promo))
                    else:
                        out.append(Move(start, Position(cx, cy), None))

        # en passant (geometry only; self-check filtered later)
        if self.en_passant_target:
            ex, ey = self.en_passant_target.x, self.en_passant_target.y
            if ey == sy + dirc and abs(ex - sx) == 1:
                out.append(Move(start, Position(ex, ey), None))

        return out

    def _pseudo_knight(self, start: Position, color: Color):
        sx, sy = start.x, start.y
        out = []
        for dx, dy in _KNIGHT:
            x, y = sx + dx, sy + dy
            if 0 <= x < COLUMN_SIZE and 0 <= y < ROW_SIZE:
                p = self.grid[y][x]
                if p is None or p.color != color:
                    out.append(Move(start, Position(x, y), None))
        return out

    def _pseudo_king(self, start: Position, color: Color):
        sx, sy = start.x, start.y
        out = []
        for dx, dy in _KING:
            x, y = sx + dx, sy + dy
            if 0 <= x < COLUMN_SIZE and 0 <= y < ROW_SIZE:
                p = self.grid[y][x]
                if p is None or p.color != color:
                    out.append(Move(start, Position(x, y), None))

        # castling via can_castle (re-uses your checks)
        # king from must be on home square or can_castle will fail anyway
        # Try K-side and Q-side destinations
        for dest_x in (6, 2):
            dest = Position(dest_x, sy)
            if self.can_castle(color, start, dest):
                out.append(Move(start, dest, None))

        return out

    def _pseudo_slider(self, start: Position, color: Color, dirs):
        sx, sy = start.x, start.y
        out = []
        for dx, dy in dirs:
            x, y = sx + dx, sy + dy
            while 0 <= x < COLUMN_SIZE and 0 <= y < ROW_SIZE:
                p = self.grid[y][x]
                if p is None:
                    out.append(Move(start, Position(x, y), None))
                else:
                    if p.color != color:
                        out.append(Move(start, Position(x, y), None))
                    break
                x += dx; y += dy
        return out

    def _pseudo_moves_from(self, start: Position):
        piece = self.get_piece(start)
        if not piece:
            return []
        t, c = piece.type, piece.color
        if t == PieceType.PAWN:   return self._pseudo_pawn(start, c)
        if t == PieceType.KNIGHT: return self._pseudo_knight(start, c)
        if t == PieceType.BISHOP: return self._pseudo_slider(start, c, _DIRS_B)
        if t == PieceType.ROOK:   return self._pseudo_slider(start, c, _DIRS_R)
        if t == PieceType.QUEEN:  return self._pseudo_slider(start, c, _DIRS_Q)
        if t == PieceType.KING:   return self._pseudo_king(start, c)
        return []



    def create_default_board(self):
        for x in range(COLUMN_SIZE):
            self.grid[1][x] = Pawn(Color.WHITE)
            self.grid[6][x] = Pawn(Color.BLACK)
        
        placements = [
            (Rook, 0), (Knight, 1), (Bishop, 2), (Queen, 3),
            (King, 4), (Bishop, 5), (Knight, 6), (Rook, 7)
        ]
        
        for piece_class, x in placements:
            self.grid[0][x] = piece_class(Color.WHITE)
            self.grid[7][x] = piece_class(Color.BLACK)

    def can_castle(self, color: Color, start: Position, end: Position) -> bool:
        if (end.y != start.y) or (abs(end.x - start.x) != 2):
            return False

        rank = 0 if color == Color.WHITE else ROW_SIZE - 1
        k_from_x, k_to_x = 4, end.x
        kside = (k_to_x > k_from_x)            # True = king-side, False = queen-side
        side = 'K' if kside else 'Q'
        if not self.castling_rights[color][side]:
            return False

        r_from_x = (COLUMN_SIZE - 1) if kside else 0
        r_to_x   = 5 if kside else 3
        king_path_x = [5, 6] if kside else [3, 2]            # squares king passes/lands on
        between_x   = king_path_x if kside else [3, 2, 1]    # must all be empty

        king = self.get_piece(Position(k_from_x, rank))
        rook = self.get_piece(Position(r_from_x, rank))
        if not (king and king.type == PieceType.KING and king.color == color):
            return False
        if not (rook and rook.type == PieceType.ROOK and rook.color == color):
            return False
        if any(not self.is_empty(Position(x, rank)) for x in between_x):
            return False

        opp = Color.BLACK if color == Color.WHITE else Color.WHITE
        if self.is_square_attacked(Position(k_from_x, rank), opp):
            return False
        if any(self.is_square_attacked(Position(x, rank), opp) for x in king_path_x):
            return False
        return True
    
    def castle(self, color: Color, start: Position, end: Position) -> bool:
        if not self.can_castle(color, start, end):
            return False

        side = 'K' if end.x > start.x else 'Q'
        rank = start.y
        r_from_x = (COLUMN_SIZE - 1) if side == 'K' else 0
        r_to_x   = 5 if side == 'K' else 3

        self.move(start, end)
        self.move(Position(r_from_x, rank), Position(r_to_x, rank))

        self.castling_rights[color]['K'] = False
        self.castling_rights[color]['Q'] = False
        self.en_passant_target = None
        return True

    def place_piece(self, piece: Piece, position: Position):
        self.grid[position.y][position.x] = piece

    def clone(self) -> "Board":
        """Deep copy: new grid cells and fresh piece instances (same types/colors)."""
        makers = {
            PieceType.PAWN: Pawn,
            PieceType.KNIGHT: Knight,
            PieceType.BISHOP: Bishop,
            PieceType.ROOK: Rook,
            PieceType.QUEEN: Queen,
            PieceType.KING: King,
        }
        nb = Board.__new__(Board)
        nb.grid = [[None for _ in range(COLUMN_SIZE)] for _ in range(ROW_SIZE)]
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                p = self.grid[y][x]
                if p is not None:
                    nb.grid[y][x] = makers[p.type](p.color)
        nb.en_passant_target = (
            Position(self.en_passant_target.x, self.en_passant_target.y)
            if self.en_passant_target is not None
            else None
        )
        nb.castling_rights = {
            Color.WHITE: self.castling_rights[Color.WHITE].copy(),
            Color.BLACK: self.castling_rights[Color.BLACK].copy(),
        }
        return nb

    # NOTE: Does not check legality!
    def move(self, start: Position, end: Position):
        piece = self.get_piece(start)
        captured = self.get_piece(end)
        self.grid[end.y][end.x] = piece
        self.grid[start.y][start.x] = None
        return captured

    def _revert_raw(self, start: Position, end: Position, captured: Optional[Piece]):
        piece = self.get_piece(end)
        self.grid[start.y][start.x] = piece
        self.grid[end.y][end.x] = captured

    def legal_moves_from(self, start: Position) -> List[Move]:
        piece = self.get_piece(start)
        if not piece:
            return []
        color = piece.color
        legal = []
        for mv in self._pseudo_moves_from(start):
            if self.is_legal_move(mv.start, mv.end, color):
                legal.append(mv)
        return legal

    def has_any_legal_moves(self, color: Color) -> bool:
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                p = self.grid[y][x]
                if p and p.color == color:
                    if self.legal_moves_from(Position(x, y)):
                        return True
        return False


    def is_legal_move(self, start: Position, end: Position, turn: Color) -> bool:
        if start.x == end.x and start.y == end.y:
            return False  # no-op
        piece = self.get_piece(start)
        if not piece or piece.color != turn:
            return False

        target = self.get_piece(end)
        if target and target.color == piece.color:
            return False

        color = piece.color
        opp   = Color.BLACK if color == Color.WHITE else Color.WHITE
        dx, dy = end.x - start.x, end.y - start.y

        # 1) Castling (king moves two squares horizontally)
        if piece.type == PieceType.KING and start.y == end.y and abs(dx) == 2:
            return self.can_castle(color, start, end)

        # 2) En passant (pawn moves diagonally into an empty square that matches en_passant_target)
        is_ep = False
        ep_captured_at = None
        if piece.type == PieceType.PAWN and abs(dx) == 1:
            direction = 1 if color == Color.WHITE else -1
            ep = self.en_passant_target
            if dy == direction and target is None and ep and end.x == ep.x and end.y == ep.y:
                ep_captured_at = Position(end.x, end.y - direction)
                cap = self.get_piece(ep_captured_at)
                if isinstance(cap, Pawn) and cap.color == opp:
                    is_ep = True
                else:
                    return False  # bad EP geometry/state

        # 3) Normal geometry (non-EP)
        if not is_ep and not piece.valid_move(start, end, self):
            return False

        # 4) Simulate and ensure your own king isn’t left in check

        rec = self.do_move(start, end, promo=None)  # legality doesn’t depend on which promo piece
        try:
            king_pos = self.find_king(color)
            ok = king_pos is not None and not self.is_square_attacked(king_pos, opp)
        finally:
            self.undo_move(rec)

        return ok

    def in_bounds(self, p: Position) -> bool:
        return 0 <= p.x < COLUMN_SIZE and 0 <= p.y < ROW_SIZE

    def get_piece(self, position: Position) -> Optional[Piece]:
        if not self.in_bounds(position):
            return None
        return self.grid[position.y][position.x]

    def is_empty(self, position: Position) -> bool:
        return self.get_piece(position) is None
    
    def find_king(self, color: Color) -> Optional[Position]:
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                piece = self.grid[y][x]
                if piece and piece.type == PieceType.KING and piece.color == color:
                    return Position(x, y)
        return None
    def is_square_attacked(self, pos: Position, by_color: Color) -> bool:
        tx, ty = pos.x, pos.y

        # Helpers
        def _in(x, y): return 0 <= x < COLUMN_SIZE and 0 <= y < ROW_SIZE
        def _piece(x, y): return self.grid[y][x] if _in(x, y) else None

        # 1) Pawn attacks toward 'pos' (inverse check)
        dir_pawn = +1 if by_color == Color.WHITE else -1
        for dx in (-1, +1):
            x, y = tx + dx, ty - dir_pawn
            p = _piece(x, y)
            if p and p.color == by_color and p.type == PieceType.PAWN:
                return True

        # 2) Knight attacks
        KN = [(+1,+2),(+2,+1),(+2,-1),(+1,-2),(-1,-2),(-2,-1),(-2,+1),(-1,+2)]
        for dx, dy in KN:
            x, y = tx + dx, ty + dy
            p = _piece(x, y)
            if p and p.color == by_color and p.type == PieceType.KNIGHT:
                return True

        # 3) King attacks (adjacent)
        K1 = [(-1,-1),(-1,0),(-1,+1),(0,-1),(0,+1),(+1,-1),(+1,0),(+1,+1)]
        for dx, dy in K1:
            x, y = tx + dx, ty + dy
            p = _piece(x, y)
            if p and p.color == by_color and p.type == PieceType.KING:
                return True

        # 4) Sliding attacks
        BDIRS = [(-1,-1),(-1,+1),(+1,-1),(+1,+1)]
        RDIRS = [(-1,0),(+1,0),(0,-1),(0,+1)]

        # Diagonals (bishop/queen)
        for dx, dy in BDIRS:
            x, y = tx + dx, ty + dy
            while _in(x, y):
                p = _piece(x, y)
                if p:
                    if p.color == by_color and (p.type == PieceType.BISHOP or p.type == PieceType.QUEEN):
                        return True
                    break
                x += dx; y += dy

        # Orthogonals (rook/queen)
        for dx, dy in RDIRS:
            x, y = tx + dx, ty + dy
            while _in(x, y):
                p = _piece(x, y)
                if p:
                    if p.color == by_color and (p.type == PieceType.ROOK or p.type == PieceType.QUEEN):
                        return True
                    break
                x += dx; y += dy

        return False

    # objects.py (inside class Board)
    def do_move(self, start: "Position", end: "Position", promo: Optional["PieceType"]) -> "MoveRecord":
        """
        Execute a (legal) move on the board in-place and return a record
        that can be used to perfectly undo this move.
        DOES NOT change Game.turn/fullmove_number. Only board state here.
        """
        rec = MoveRecord(
            start=start, end=end,
            moved_piece=self.get_piece(start),
            captured_piece=None, ep_captured_at=None,
            was_promotion=False, promo_piece=None,
            rook_start=None, rook_end=None,
            prev_en_passant=self.en_passant_target,
            prev_castling_rights={
                Color.WHITE: self.castling_rights[Color.WHITE].copy(),
                Color.BLACK: self.castling_rights[Color.BLACK].copy(),
            },
            prev_halfmove_clock=getattr(self, 'halfmove_clock', 0)  # if Board tracks it
        )

        moving = rec.moved_piece
        assert moving is not None, "No piece on start square"

        # Halfmove clock bookkeeping (if Board tracks it; if Game tracks it, ignore here)
        # reset on pawn move or capture
        is_pawn_move = (moving.type == PieceType.PAWN)

        # Clear en-passant by default (will set below on double push)
        self.en_passant_target = None

        # Detect en-passant capture
        ep_capture_pos = None
        if is_pawn_move and (end.x != start.x) and (self.get_piece(end) is None):
            # diagonal move to empty square ⇒ en-passant capture
            dy = 1 if moving.color == Color.WHITE else -1
            ep_capture_pos = Position(end.x, end.y - dy)
            rec.captured_piece = self.get_piece(ep_capture_pos)
            rec.ep_captured_at = ep_capture_pos
            # remove the captured pawn
            self.grid[ep_capture_pos.y][ep_capture_pos.x] = None

        # Normal capture (not EP)
        if rec.captured_piece is None:
            rec.captured_piece = self.get_piece(end)

        # Move the piece
        self.grid[start.y][start.x] = None
        self.grid[end.y][end.x] = moving

        # Handle castling (king moves 2 squares)
        if moving.type == PieceType.KING and abs(end.x - start.x) == 2 and end.y == start.y:
            kside = (end.x > start.x)  # True if king-side
            rank = start.y
            r_from_x = (COLUMN_SIZE - 1) if kside else 0
            r_to_x   = 5 if kside else 3
            rec.rook_start = Position(r_from_x, rank)
            rec.rook_end   = Position(r_to_x, rank)
            rook = self.get_piece(rec.rook_start)
            # move rook
            self.grid[rank][r_from_x] = None
            self.grid[rank][r_to_x] = rook
            # clear castling rights for this color
            self.castling_rights[moving.color]["K"] = False
            self.castling_rights[moving.color]["Q"] = False

        # Promotion (if requested)
        if is_pawn_move:
            # set new en-passant target on double push
            if abs(end.y - start.y) == 2:
                mid_y = (start.y + end.y) // 2
                self.en_passant_target = Position(start.x, mid_y)

            last_rank = 7 if moving.color == Color.WHITE else 0
            if end.y == last_rank and promo is not None:
                rec.was_promotion = True
                # replace pawn with the promoted piece
                if promo == PieceType.QUEEN:
                    rec.promo_piece = Queen(moving.color)
                elif promo == PieceType.ROOK:
                    rec.promo_piece = Rook(moving.color)
                elif promo == PieceType.BISHOP:
                    rec.promo_piece = Bishop(moving.color)
                elif promo == PieceType.KNIGHT:
                    rec.promo_piece = Knight(moving.color)
                else:
                    raise ValueError("Unsupported promotion type")
                self.grid[end.y][end.x] = rec.promo_piece

        # Update castling rights when king/rooks move or a rook is captured on its original square
        if moving.type == PieceType.KING:
            self.castling_rights[moving.color]["K"] = False
            self.castling_rights[moving.color]["Q"] = False

        if moving.type == PieceType.ROOK:
            if moving.color == Color.WHITE and start.y == 0:
                if start.x == 0: self.castling_rights[Color.WHITE]["Q"] = False
                if start.x == 7: self.castling_rights[Color.WHITE]["K"] = False
            if moving.color == Color.BLACK and start.y == 7:
                if start.x == 0: self.castling_rights[Color.BLACK]["Q"] = False
                if start.x == 7: self.castling_rights[Color.BLACK]["K"] = False

        # If a rook was captured on its home square, clear that side's right
        if rec.captured_piece is not None and rec.ep_captured_at is None:
            cp = rec.captured_piece
            if cp.type == PieceType.ROOK:
                if end == Position(0,0): self.castling_rights[Color.WHITE]["Q"] = False
                if end == Position(7,0): self.castling_rights[Color.WHITE]["K"] = False
                if end == Position(0,7): self.castling_rights[Color.BLACK]["Q"] = False
                if end == Position(7,7): self.castling_rights[Color.BLACK]["K"] = False

        return rec

    def undo_move(self, rec: "MoveRecord") -> None:
        """Perfectly revert the board to the state before do_move(rec)."""
        # Undo promotion: replace promoted piece back with pawn before moving back
        if rec.was_promotion:
            # put a pawn of the same color back on end
            pawn_color = rec.moved_piece.color
            self.grid[rec.end.y][rec.end.x] = Pawn(pawn_color)

        # Move piece back
        self.grid[rec.start.y][rec.start.x] = rec.moved_piece
        self.grid[rec.end.y][rec.end.x] = None

        # Restore rook if castling
        if rec.rook_start and rec.rook_end:
            rook = self.get_piece(rec.rook_end)
            self.grid[rec.rook_end.y][rec.rook_end.x] = None
            self.grid[rec.rook_start.y][rec.rook_start.x] = rook

        # Restore captured piece (EP or normal)
        if rec.captured_piece is not None:
            if rec.ep_captured_at is not None:
                self.grid[rec.ep_captured_at.y][rec.ep_captured_at.x] = rec.captured_piece
            else:
                self.grid[rec.end.y][rec.end.x] = rec.captured_piece

        # Restore board-level state
        self.en_passant_target = rec.prev_en_passant
        self.castling_rights = {
            Color.WHITE: rec.prev_castling_rights[Color.WHITE].copy(),
            Color.BLACK: rec.prev_castling_rights[Color.BLACK].copy(),
        }
            
