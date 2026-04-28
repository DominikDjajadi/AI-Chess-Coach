from collections import defaultdict
from objects import Color, Position, Piece, PieceType, Board, ROW_SIZE, COLUMN_SIZE, Outcome, MoveRecord, Move
from typing import Optional, Tuple, List, NamedTuple
from dataclasses import dataclass
from objects import Queen, Rook, Bishop, Knight

PROMOTABLE = {PieceType.QUEEN, PieceType.ROOK, PieceType.BISHOP, PieceType.KNIGHT}

class MoveRec(NamedTuple):
    start: Position
    end: Position
    promotion: Optional[PieceType]
    color: Color            # mover’s color

@dataclass
class PlyState:
    move_rec: "MoveRecord"
    prev_turn: "Color"
    prev_fullmove: int
    prev_halfmove_clock: int  # if you store this on Game; if it's on Board, omit

@dataclass
class Game:
    board: Board
    turn: Color
    halfmove_clock: int = 0
    fullmove_number: int = 1

    def __init__(self, board: Optional[Board] = None, turn: Color = Color.WHITE):
        self.board = board if board else Board()
        self.turn = turn
        self.halfmove_clock = 0
        self.fullmove_number = 1
        self._rep_counts = defaultdict(int)
        self._rep_counts[self._position_key()] += 1
        self._move_history: list[MoveRec] = []


    def make_move(self, start: Position, end: Position, promotion: Optional[PieceType] = None) -> bool:
        # Basic side-to-move & geometry legality
        piece = self.board.get_piece(start)
        if not piece or piece.color != self.turn:
            return False
        if not self.board.is_legal_move(start, end, self.turn):
            return False

        # Choose promotion piece (default queen)
        if piece.type == PieceType.PAWN:
            last_rank = ROW_SIZE - 1 if piece.color == Color.WHITE else 0
            if end.y == last_rank and promotion not in PROMOTABLE:
                promotion = PieceType.QUEEN

        _ = self.push(Move(start, end, promotion))  # push handles history + repetition
        return True


    def _position_key(self):
        """Hashable key for repetition detection: pieces + side + castle + EP."""
        grid_key = tuple(
            tuple(
                None if (p := self.board.grid[y][x]) is None else (p.type.value, p.color.value)
                for x in range(COLUMN_SIZE)
            )
            for y in range(ROW_SIZE)
        )
        cr = self.board.castling_rights
        cr_key = (cr[Color.WHITE]['K'], cr[Color.WHITE]['Q'], cr[Color.BLACK]['K'], cr[Color.BLACK]['Q'])
        ep = self.board.en_passant_target
        ep_key = None if ep is None else (ep.x, ep.y)
        return (grid_key, self.turn.value, cr_key, ep_key)

    def _insufficient_material(self) -> bool:
        """Very small, safe subset: K vs K, K vs K+minor (single N/B)."""
        minors = 0
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                p = self.board.grid[y][x]
                if p is None:
                    continue
                if p.type in (PieceType.PAWN, PieceType.ROOK, PieceType.QUEEN):
                    return False
                if p.type in (PieceType.BISHOP, PieceType.KNIGHT):
                    minors += 1
                    if minors > 1:
                        return False
        return True  # K vs K or K vs K+single minor

    def _apply_promotion(self, square: Position, color: Color, choice: PieceType) -> None:
        """Replace pawn at `square` with the chosen piece."""
        if choice == PieceType.QUEEN:
            self.board.grid[square.y][square.x] = Queen(color)
        elif choice == PieceType.ROOK:
            self.board.grid[square.y][square.x] = Rook(color)
        elif choice == PieceType.BISHOP:
            self.board.grid[square.y][square.x] = Bishop(color)
        elif choice == PieceType.KNIGHT:
            self.board.grid[square.y][square.x] = Knight(color)

    def is_in_check(self, color: Color) -> bool:
        king_pos = self.board.find_king(color)
        if king_pos is None:
            return False
        opp = Color.BLACK if color == Color.WHITE else Color.WHITE
        return self.board.is_square_attacked(king_pos, opp)
    
    def legal_moves_from(self, position: Position, color: Optional[Color] = None) -> List[Move]:
        """
        Legal moves for the piece on `position` (as Move objects), filtered for self-check.
        Uses Board.legal_moves_from for speed.
        """
        color = self.turn if color is None else color
        piece = self.board.get_piece(position)
        if not piece or piece.color != color:
            return []
        return self.board.legal_moves_from(position)

    def has_any_legal_moves(self, color: Color) -> bool:
        return self.board.has_any_legal_moves(color)
        
    def legal_moves(self, color: Optional[Color] = None) -> List[Move]:
        """ 
        All legal moves for `color` (default: side to move).
        """
        color = self.turn if color is None else color
        out: List[Move] = []
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                pos = Position(x, y)
                p = self.board.get_piece(pos)
                if p and p.color == color:
                    out.extend(self.board.legal_moves_from(pos))
        return out

    def result(self) -> Optional[Outcome]:
        # 50-move rule (100 halfmoves)
        if self.halfmove_clock >= 100:
            return Outcome.DRAW
        # Threefold repetition
        if self._rep_counts[self._position_key()] >= 3:
            return Outcome.DRAW
        # Insufficient material (simple subset)
        if self._insufficient_material():
            return Outcome.DRAW

        in_check = self.is_in_check(self.turn)
        if self.has_any_legal_moves(self.turn):
            return Outcome.ONGOING
        if in_check:
            return Outcome.WHITE_WIN if self.turn == Color.BLACK else Outcome.BLACK_WIN
        return Outcome.DRAW

    
    def is_terminal(self) -> Tuple[bool, Optional[Outcome]]:
        res = self.result()
        return (res != Outcome.ONGOING), (None if res == Outcome.ONGOING else res)

    def copy(self) -> "Game":
        # Manual, fast copy leveraging Board.clone()
        g = Game.__new__(Game)   # bypass __init__
        g.board = self.board.clone()
        g.turn = self.turn
        g.halfmove_clock = self.halfmove_clock
        g.fullmove_number = self.fullmove_number
        g._move_history = self._move_history.copy()
        g._rep_counts = self._rep_counts.copy()
        return g
    
    def push(self, move: "Move") -> "PlyState":
        """
        In-place apply a move at the Game level.
        Returns a PlyState you must pass to pop() to undo.
        """
        mover = self.turn  # color BEFORE applying

        # Apply on the board (returns a MoveRecord describing what happened)
        rec = self.board.do_move(move.start, move.end, move.promo)

        # Save state for undo
        st = PlyState(
            move_rec=rec,
            prev_turn=self.turn,
            prev_fullmove=self.fullmove_number,
            prev_halfmove_clock=self.halfmove_clock
        )

        # === Update game meta
        # halfmove clock: reset on pawn move or capture
        moved = rec.moved_piece
        made_capture = rec.captured_piece is not None
        if moved.type == PieceType.PAWN or made_capture:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        # flip side to move
        self.turn = Color.BLACK if self.turn == Color.WHITE else Color.WHITE
        # fullmove increments AFTER Black's move
        if self.turn == Color.WHITE:
            self.fullmove_number += 1

        # === History & repetition
        # record last move (with mover color)
        self._move_history.append(MoveRec(move.start, move.end, move.promo, mover))

        # repetition count for the **new** position (after flipping turn etc.)
        key = self._position_key()
        self._rep_counts[key] += 1

        return st

    def pop(self, state: "PlyState") -> None:
        """Undo the last push() by restoring Game meta and Board position."""
        # Decrement repetition for the current position (the one we are about to undo)
        curr_key = self._position_key()
        if curr_key in self._rep_counts:
            self._rep_counts[curr_key] -= 1
            if self._rep_counts[curr_key] <= 0:
                del self._rep_counts[curr_key]

        # Restore meta
        self.turn = state.prev_turn
        self.fullmove_number = state.prev_fullmove
        self.halfmove_clock = state.prev_halfmove_clock

        # Revert board
        self.board.undo_move(state.move_rec)

        # Drop last move from history
        if self._move_history:
            self._move_history.pop()

    
    def last_move(self) -> Optional["MoveRec"]:
        return self._move_history[-1] if self._move_history else None

    def position_key(self):
        """Public alias for the post-move position key (pieces + stm + castle + EP)."""
        return self._position_key()

    def repetition_count_for_key(self, key):
        return int(self._rep_counts.get(key, 0))

    def would_repeat_count(self, start, end, promotion=None) -> int:
        """
        Repetition count of the *resulting* position if we play (start,end,promotion).
        """
        st = self.push(Move(start, end, promotion))
        key = self.position_key()
        rep = self.repetition_count_for_key(key)
        self.pop(st)
        return rep

    def _clear_board(self) -> None:
        # wipe the 8x8 grid
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                self.board.grid[y][x] = None  # direct clear

    def _place_piece(self, x: int, y: int, symbol: str) -> None:
        # map FEN char -> Piece instance (color from case)
        col = Color.WHITE if symbol.isupper() else Color.BLACK
        s = symbol.lower()
        from objects import Pawn, Knight, Bishop, Rook, Queen, King
        piece = {
            'p': Pawn, 'n': Knight, 'b': Bishop, 'r': Rook, 'q': Queen, 'k': King
        }[s](col)
        # you have Board.place_piece; use it for clarity
        self.board.place_piece(piece, Position(x, y))  # writes into grid
        # (Board.place_piece implementation writes to grid[y][x])  # docs note

    def set_fen(self, fen: str | None) -> None:
        """
        Load a position from FEN.
        Accepts None or "startpos" to reset to the initial position.
        """
        # 0) startpos / None → reset
        if fen is None or fen == "startpos":
            # Re-init to your standard start position
            self.__init__()
            return

        fen = fen.strip()
        parts = fen.split()
        if len(parts) < 4:
            raise ValueError(f"Bad FEN (need ≥4 fields): {fen}")

        placement, side, castling, ep = parts[:4]
        halfmove = int(parts[4]) if len(parts) > 4 else 0
        fullmove = int(parts[5]) if len(parts) > 5 else 1

        # 1) clear board grid
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                self.board.grid[y][x] = None

        # 2) place pieces (FEN ranks 8→1 map to y=7→0)
        from objects import Pawn, Knight, Bishop, Rook, Queen, King, Color, PieceType, Position
        char2cls = {'p': Pawn, 'n': Knight, 'b': Bishop, 'r': Rook, 'q': Queen, 'k': King}
        ranks = placement.split('/')
        if len(ranks) != ROW_SIZE:
            raise ValueError("Bad piece placement in FEN")
        for r_i, row in enumerate(ranks):
            y = ROW_SIZE - 1 - r_i
            x = 0
            for ch in row:
                if ch.isdigit():
                    x += int(ch)
                else:
                    col = Color.WHITE if ch.isupper() else Color.BLACK
                    piece = char2cls[ch.lower()](col)
                    self.board.place_piece(piece, Position(x, y))
                    x += 1
            if x != COLUMN_SIZE:
                raise ValueError(f"Bad FEN rank width: {row}")

        # 3) side to move
        self.turn = Color.WHITE if side == 'w' else Color.BLACK

        # 4) castling rights (support both dict style and boolean attrs)
        if hasattr(self.board, "castling_rights"):
            cr = self.board.castling_rights
            # If dict-of-dicts keyed by Color and 'K'/'Q'
            try:
                cr[Color.WHITE]['K'] = 'K' in castling
                cr[Color.WHITE]['Q'] = 'Q' in castling
                cr[Color.BLACK]['K'] = 'k' in castling
                cr[Color.BLACK]['Q'] = 'q' in castling
            except Exception:
                pass
        # Also try common boolean fields if present
        for attr, present in (
            ("white_can_castle_k", 'K' in castling),
            ("white_can_castle_q", 'Q' in castling),
            ("black_can_castle_k", 'k' in castling),
            ("black_can_castle_q", 'q' in castling),
        ):
            if hasattr(self.board, attr):
                setattr(self.board, attr, present)

        # 5) en-passant target square
        if ep == "-":
            # your engine typically stores EP on the Board
            if hasattr(self.board, "en_passant_target"):
                self.board.en_passant_target = None
            else:
                self.en_passant_target = None
        else:
            fx = ord(ep[0]) - ord('a')      # file a..h → 0..7
            ry = int(ep[1]) - 1             # rank 1..8 → 0..7
            target = Position(fx, ry)
            if hasattr(self.board, "en_passant_target"):
                self.board.en_passant_target = target
            else:
                self.en_passant_target = target

        # 6) clocks
        self.halfmove_clock = halfmove
        self.fullmove_number = fullmove

        # 7) (optional) reset repetition tracking if you maintain one
        if hasattr(self, "_rep_counts"):
            self._rep_counts.clear()
            if hasattr(self, "_position_key"):
                self._rep_counts[self._position_key()] = 1

    def fen(self) -> str:
        """
        Serialize the current position to a 6-field FEN string (round-trips with set_fen).
        """
        def piece_char(p: Piece) -> str:
            ch = {
                PieceType.PAWN: "p",
                PieceType.KNIGHT: "n",
                PieceType.BISHOP: "b",
                PieceType.ROOK: "r",
                PieceType.QUEEN: "q",
                PieceType.KING: "k",
            }[p.type]
            return ch.upper() if p.color == Color.WHITE else ch

        rows: List[str] = []
        for y in range(ROW_SIZE - 1, -1, -1):
            run = 0
            parts: List[str] = []
            for x in range(COLUMN_SIZE):
                p = self.board.grid[y][x]
                if p is None:
                    run += 1
                else:
                    if run:
                        parts.append(str(run))
                        run = 0
                    parts.append(piece_char(p))
            if run:
                parts.append(str(run))
            rows.append("".join(parts))
        placement = "/".join(rows)

        stm = "w" if self.turn == Color.WHITE else "b"

        cr = self.board.castling_rights
        cstr = ""
        if cr[Color.WHITE]["K"]:
            cstr += "K"
        if cr[Color.WHITE]["Q"]:
            cstr += "Q"
        if cr[Color.BLACK]["K"]:
            cstr += "k"
        if cr[Color.BLACK]["Q"]:
            cstr += "q"
        castling = cstr if cstr else "-"

        ep = "-"
        et = getattr(self.board, "en_passant_target", None)
        if et is not None:
            ep = chr(ord("a") + et.x) + str(et.y + 1)

        return f"{placement} {stm} {castling} {ep} {self.halfmove_clock} {self.fullmove_number}"

    def material_balance(self) -> int:
        """
        Positive means White is ahead.
        Negative means Black is ahead.
        """
        values = {
            PieceType.PAWN: 1,
            PieceType.KNIGHT: 3,
            PieceType.BISHOP: 3,
            PieceType.ROOK: 5,
            PieceType.QUEEN: 9,
            PieceType.KING: 0,
        }

        score = 0
        for y in range(ROW_SIZE):
            for x in range(COLUMN_SIZE):
                p = self.board.grid[y][x]
                if p is None:
                    continue
                v = values[p.type]
                if p.color == Color.WHITE:
                    score += v
                else:
                    score -= v
        return score




def ascii_board(board: Board) -> str:
    symbols = {
        (PieceType.PAWN,   Color.WHITE): 'P',
        (PieceType.ROOK,   Color.WHITE): 'R',
        (PieceType.KNIGHT, Color.WHITE): 'N',
        (PieceType.BISHOP, Color.WHITE): 'B',
        (PieceType.QUEEN,  Color.WHITE): 'Q',
        (PieceType.KING,   Color.WHITE): 'K',
        (PieceType.PAWN,   Color.BLACK): 'p',
        (PieceType.ROOK,   Color.BLACK): 'r',
        (PieceType.KNIGHT, Color.BLACK): 'n',
        (PieceType.BISHOP, Color.BLACK): 'b',
        (PieceType.QUEEN,  Color.BLACK): 'q',
        (PieceType.KING,   Color.BLACK): 'k',
    }
    rows = []
    for y in range(ROW_SIZE-1, -1, -1):
        row = []
        for x in range(COLUMN_SIZE):
            p = board.get_piece(Position(x, y))
            row.append(symbols.get((p.type, p.color), '.') if p else '.')
        rows.append(' '.join(row))
    return '\n'.join(rows)
