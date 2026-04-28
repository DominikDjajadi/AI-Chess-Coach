# AI Chess

AI Chess is a Flask-based chess web application for playing against Stockfish, analyzing chess positions, editing board positions, explaining candidate moves, and reviewing full games from PGN.

The project combines:

- a custom chess rules/model layer,
- a Flask backend API,
- a browser-based chess UI,
- Stockfish engine integration,
- deterministic move explanation logic, and
- full-game review tools.

## Open-source code and third-party components used

This project uses the following open-source or third-party components:

| Component | How it is used |
| --- | --- |
| **Stockfish** | Used as the external UCI chess engine for play moves, engine evaluations, candidate move ranking, and principal variation generation. |
| **python-chess** | Used for UCI communication with Stockfish and for PGN/FEN-related backend analysis utilities. |
| **Flask** | Used to serve the web application and expose JSON API endpoints. |
| **python-dotenv** | Used to load optional local configuration from a `.env` file. |
| **chess.js** | Loaded in the frontend for client-side chess move/FEN handling and SAN/PV display. |
| **Google Fonts / Source Sans 3** | Used for the web UI font. |

## Changes made to imported open-source code

I did not directly modify the source code of Stockfish, Flask, python-chess, chess.js, python-dotenv, or Google Fonts.

Instead, I integrated these tools into my own application code. The main integration work was:

- wrapping Stockfish through a reusable Python engine interface,
- configuring Stockfish strength, depth/time limits, hash size, threads, and optional Syzygy paths,
- using python-chess to communicate with Stockfish and parse PGNs,
- exposing Stockfish-backed analysis through Flask API endpoints,
- using chess.js only as a client-side helper for board/FEN/move display,
- building my own UI and application behavior around those tools.

If the submitted repository includes a bundled Stockfish binary or source folder, the original Stockfish license and attribution files should remain with it.

## New code implemented

### Custom chess model

The files `game.py`, `objects.py`, and `chess_uci.py` implement a custom chess model, including:

- board representation,
- piece classes,
- legal move generation,
- side-to-move tracking,
- FEN import/export,
- UCI conversion helpers,
- move history,
- push/pop move state,
- check and terminal-state detection,
- castling,
- en passant,
- promotion,
- draw conditions, and
- a mate-in-one helper used before falling back to Stockfish in play mode.

### Flask backend

The Flask backend implements routes and API endpoints for the web app, including:

- serving the main UI,
- creating and updating play sessions,
- making player and engine moves,
- exporting PGNs,
- analyzing positions,
- explaining moves,
- reviewing full games, and
- generating principal variation lines.

The backend also supports environment-variable configuration for Stockfish path, depth, analysis strength, cache sizes, session lifetime, review limits, and play Elo limits.

### Stockfish service layer

The Stockfish integration code implements:

- a process-local Stockfish engine wrapper,
- serialized/thread-safe UCI access,
- engine startup and shutdown handling,
- configurable Stockfish options,
- play-strength limiting,
- analysis-depth/time controls,
- candidate move analysis,
- principal variation generation, and
- caching for repeated analysis/PV requests.

### Move explanation system

The move explanation system is custom and deterministic. It does not use an LLM or external text-generation API.

It analyzes candidate moves using chess-specific heuristics such as:

- checks,
- checkmate,
- captures,
- material gain/loss,
- static exchange evaluation,
- piece safety,
- forks,
- development,
- castling,
- king safety,
- passed pawns,
- opened lines,
- blocking own pieces,
- hanging pieces, and
- missed tactics.

It classifies moves with quality labels such as:

- `best`,
- `excellent`,
- `good`,
- `playable`,
- `inaccuracy`,
- `mistake`, and
- `blunder`.

### Full-game review

The full-game reviewer is also custom. It accepts PGN text, walks through the main line, compares each move against Stockfish's preferred move, and produces:

- per-side move quality counts,
- average centipawn loss,
- key mistakes,
- recurring themes,
- coaching suggestions, and
- side-specific summaries.

The reviewer reuses the same move-explanation heuristics so that the single-move explanation feature and the full-game review feature stay consistent.

### Frontend UI

The frontend implements the browser interface for:

- Play mode,
- Analyze mode,
- Review mode,
- About/help content,
- chessboard rendering,
- move selection,
- legal move display,
- game status messages,
- PGN copy/download actions,
- analysis result display,
- candidate move tables,
- principal variation controls,
- clickable PV lines,
- move explanation display,
- PGN review navigation, and
- toast/status feedback.

### Board editor

The board editor is custom and includes:

- piece placement tools,
- sticky selected tools,
- neutral drag-to-move behavior,
- eraser mode,
- right-click clearing,
- undo support,
- side-to-move controls,
- castling-rights controls,
- real-time FEN rebuilding,
- position validation,
- king-count checks,
- pawn-placement checks,
- piece-count checks,
- castling consistency checks, and
- check-state consistency checks.

## Nontrivial work completed

This project goes beyond simply embedding Stockfish in a web page. The main nontrivial work includes:

- building a complete Play / Analyze / Review workflow,
- implementing a custom chess rules layer,
- integrating Stockfish through a configurable backend service,
- adding local game sessions and PGN export,
- creating engine-backed candidate move analysis,
- generating principal variation lines,
- implementing deterministic move explanations,
- implementing full-game PGN review,
- creating an interactive board editor,
- validating edited positions in real time,
- building a multi-tab frontend interface, and
- connecting frontend interactions to backend analysis APIs.
