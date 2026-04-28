# Critical invariants (training correctness)

These are the properties that must hold for self-play and training to be correct. When changing MCTS, game, trainer, or self-play, verify these still hold.

---

## MCTS (mcts.py)

- **Value storage**: In every node, `Q` (and `W/N`) is the expected outcome **for the side to move in that node** (mover-centric). Backup alternates sign along the path so each node gets the correct perspective.
- **Selection**: When choosing a move at a node, we maximize **the current player's** value. The child node is the state *after* the current player's move, so the side to move in the child is the opponent. Therefore UCB must use **-child.Q** (not child.Q) so that good moves for the current player get higher scores.
- **Terminal (mate/stalemate)**: We do not expand the terminal node. We backup the terminal value (e.g. -1 for checkmate for the side to move there) along the path. The edge that led to the terminal gets updated; that move should then be preferred by selection at the parent (after the sign fix above).

---

## Policy and value targets

- **Policy target**: The target for the policy head in training is the **MCTS visit distribution at the root** (normalized visit counts over root children), from the same position that produced the training sample. Not the raw net prior.
- **Value target**: The target for the value head is the **game outcome** (win/loss/draw, e.g. +1/-1/0) from the perspective of the **side to move in that position**.

---

## Game / terminal

- **Terminal outcomes**: Checkmate, stalemate, 50-move, repetition, and (if used) move-limit adjudication must be detected and reported so that the value target (game result) is correct for each position in the game.

---

*Last updated when adding UCB sign fix and initial invariant list.*
