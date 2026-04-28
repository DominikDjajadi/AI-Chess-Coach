"""
Flask web UI: play vs Stockfish + analysis tab.

  python -m webapp.app

Optional env:
  CHESS_STOCKFISH_PATH — path to the Stockfish executable (default: ``stockfish`` on PATH)
  CHESS_STOCKFISH_DEPTH — search depth for play (default 14)
  CHESS_STOCKFISH_THREADS / CHESS_STOCKFISH_HASH_MB — UCI Threads and Hash (analysis strength)
  CHESS_SYZYGY_PATH — optional Syzygy tablebase directory
  CHESS_ANALYSIS_SIMS — default ``sims`` when the request omits both ``strength`` and ``sims`` (default 64)
  CHESS_ANALYSIS_SIMS_DIVISOR — depth ≈ sims/divisor (default 2; was effectively 4 before)
  CHESS_ANALYSIS_MIN_DEPTH / CHESS_ANALYSIS_MAX_DEPTH — depth clamps (defaults 6 / 64)
  CHESS_ANALYSIS_MOVETIME_MS — if set, analysis uses this time budget instead of depth
  CHESS_PV_FALLBACK_SIMS — continuation ``sims`` for PV (default 28)
  CHESS_ANALYSIS_CACHE_SIZE / CHESS_PV_CACHE_SIZE
  CHESS_SESSION_TTL_SEC
  CHESS_PLAY_MATE_ORACLE=0 — disable mate-in-one shortcut in play mode
  CHESS_PLAY_ELO_MIN / CHESS_PLAY_ELO_MAX — clamp UCI Elo from the UI (defaults 600 / 2800)
  CHESS_REVIEW_MAX_PLIES — safety cap on PGN length for ``/api/review_game`` (default 50000)

Use one worker process or shared state if you scale out (see ``chess_service``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from game import Game

from webapp import chess_service as svc
from webapp.stockfish_engine import play_depth, stockfish_path

ALLOWED_PIECES = {
    "white-king.png",
    "white-queen.png",
    "white-rook.png",
    "white-bishop.png",
    "white-knight.png",
    "white-pawn.png",
    "black-king.png",
    "black-queen.png",
    "black-rook.png",
    "black-bishop.png",
    "black-knight.png",
    "black-pawn.png",
}


def _coerce_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# Must match webapp/static/js/app.js STRENGTH_SIMS (API may send ``strength`` without ``sims``).
_ANALYSIS_STRENGTH_SIMS = {"fast": 32, "balanced": 64, "deep": 128, "extreme": 256}


def _sims_from_strength_json(data: dict) -> Optional[int]:
    st = str(data.get("strength") or "").strip().lower()
    if not st or st == "custom":
        return None
    return _ANALYSIS_STRENGTH_SIMS.get(st)


def _analysis_sims_from_json(data: dict) -> int:
    preset = _sims_from_strength_json(data)
    if preset is not None:
        return max(1, preset)
    default = _coerce_int(os.environ.get("CHESS_ANALYSIS_SIMS", "64"), 64)
    raw = data.get("sims")
    if raw is None or raw == "":
        v = default
    else:
        v = _coerce_int(raw, default)
    return max(1, v)


def _pv_root_sims_from_json(data: dict) -> int:
    preset = _sims_from_strength_json(data)
    if preset is not None:
        return max(1, preset)
    raw = data.get("root_sims")
    if raw is None or raw == "":
        raw = data.get("analysis_sims")
    default = _coerce_int(os.environ.get("CHESS_ANALYSIS_SIMS", "64"), 64)
    if raw is None or raw == "":
        v = default
    else:
        v = _coerce_int(raw, default)
    return max(1, v)


def _fallback_sims_from_json(data: dict) -> int:
    preset = _sims_from_strength_json(data)
    if preset is not None:
        return max(12, min(120, round(preset * 0.35)))
    default = _coerce_int(os.environ.get("CHESS_PV_FALLBACK_SIMS", "28"), 28)
    raw = data.get("fallback_sims")
    if raw is None or raw == "":
        v = default
    else:
        v = _coerce_int(raw, default)
    return max(1, v)


def _stockfish_ok() -> Tuple[bool, Optional[str]]:
    try:
        import chess.engine

        from webapp.stockfish_engine import with_engine

        def ping(eng: chess.engine.SimpleEngine):
            eng.ping()

        with_engine(ping)
        return True, None
    except Exception as e:
        return False, str(e)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    pieces_dir = ROOT / "pieces-basic-png"

    @app.route("/")
    def index():
        ok, _ = _stockfish_ok()
        return render_template("index.html", stockfish_ok=ok, stockfish_path=stockfish_path())

    @app.route("/pieces/<path:name>")
    def piece_png(name: str):
        if name not in ALLOWED_PIECES:
            return ("Not found", 404)
        if not pieces_dir.is_dir():
            return ("Pieces folder missing", 404)
        return send_from_directory(pieces_dir, name, mimetype="image/png")

    @app.route("/api/health")
    def health():
        ok, err = _stockfish_ok()
        elo_min = None
        elo_max = None
        if ok:
            try:
                from webapp.stockfish_engine import engine_play_elo_bounds

                elo_min, elo_max = engine_play_elo_bounds()
            except Exception:
                elo_min = elo_max = None
        return jsonify(
            {
                "ok": ok,
                "engine": ok,
                "error": err,
                "stockfish_path": stockfish_path(),
                "play_elo_min": elo_min,
                "play_elo_max": elo_max,
            }
        )

    @app.route("/api/session", methods=["POST"])
    def api_new_session():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        human = data.get("human", "white")
        if human not in ("white", "black"):
            return jsonify({"error": "human must be 'white' or 'black'"}), 400
        start_fen = data.get("start_fen") or None
        if start_fen == "":
            start_fen = None

        elo = svc.parse_engine_elo(data.get("engine_elo"))
        sess = svc.new_session(human, start_fen, engine_elo=elo)
        depth = play_depth()
        with svc.session_mutex(sess.id):
            apply_engine_until_human(sess, depth)
            st = svc.session_state(sess)
        st["session_id"] = sess.id
        return jsonify(st)

    @app.route("/api/session/<sid>", methods=["GET", "PATCH"])
    def api_get_or_patch_session(sid: str):
        if not svc.get_session(sid):
            return jsonify({"error": "unknown session"}), 404
        if request.method == "PATCH":
            data = request.get_json(force=True, silent=True) or {}
            with svc.session_mutex(sid):
                sess = svc.SESSIONS.get(sid)
                if not sess:
                    return jsonify({"error": "unknown session"}), 404
                if "engine_elo" in data:
                    sess.engine_elo = svc.parse_engine_elo(data.get("engine_elo"))
                st = svc.session_state(sess)
            st["session_id"] = sess.id
            return jsonify(st)
        with svc.session_mutex(sid):
            sess = svc.SESSIONS.get(sid)
            if not sess:
                return jsonify({"error": "unknown session"}), 404
            st = svc.session_state(sess)
        st["session_id"] = sess.id
        return jsonify(st)

    @app.route("/api/session/<sid>/pgn", methods=["GET"])
    def api_session_pgn(sid: str):
        if not svc.get_session(sid):
            return jsonify({"error": "unknown session"}), 404
        with svc.session_mutex(sid):
            sess = svc.SESSIONS.get(sid)
            if not sess:
                return jsonify({"error": "unknown session"}), 404
            text = svc.play_session_to_pgn(sess)
        return Response(
            text,
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="game.pgn"',
                "Cache-Control": "no-store",
            },
        )

    @app.route("/api/move", methods=["POST"])
    def api_move():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        sid = data.get("session_id")
        uci = (data.get("uci") or "").strip().lower()
        if not sid or not uci:
            return jsonify({"error": "session_id and uci required"}), 400
        if not svc.get_session(sid):
            return jsonify({"error": "unknown session"}), 404

        with svc.session_mutex(sid):
            sess = svc.SESSIONS.get(sid)
            if not sess:
                return jsonify({"error": "unknown session"}), 404

            g = sess.game
            done, _ = g.is_terminal()
            if done:
                return jsonify({"error": "game already over"}), 400
            if g.turn != sess.human_color():
                return jsonify({"error": "not your turn"}), 400

            mv = svc.parse_uci_legal(g, uci)
            if mv is None:
                return jsonify({"error": "illegal uci"}), 400
            if not g.make_move(mv.start, mv.end, promotion=mv.promo):
                return jsonify({"error": "move rejected"}), 400
            sess.moves.append(uci)

            depth = play_depth()
            apply_engine_until_human(sess, depth)

            st = svc.session_state(sess)
        st["session_id"] = sess.id
        return jsonify(st)

    @app.route("/api/analyze", methods=["POST"])
    def api_analyze():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        fen = (data.get("fen") or "").strip()
        if not fen:
            return jsonify({"error": "fen required"}), 400
        main_sims = _analysis_sims_from_json(data)
        top_k = max(1, _coerce_int(data.get("top_k"), 12))
        try:
            Game().set_fen(fen)
        except Exception as e:
            return jsonify({"error": f"bad fen: {e}"}), 400

        out = svc.analyze_fen(fen, main_sims, top_k)
        return jsonify(out)

    @app.route("/api/explain_move", methods=["POST"])
    def api_explain_move():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        fen = (data.get("fen") or "").strip()
        uci = (data.get("uci") or "").strip().lower()
        if not fen or not uci:
            return jsonify({"error": "fen and uci required"}), 400
        root_sims = _pv_root_sims_from_json(data)
        raw_tp = data.get("pv_plies") or data.get("total_plies") or data.get("depth")
        pv_plies = max(1, _coerce_int(raw_tp, 6))
        try:
            Game().set_fen(fen)
        except Exception as e:
            return jsonify({"error": f"bad fen: {e}"}), 400

        from webapp.move_explainer import explain_move

        out = explain_move(fen, uci, root_sims=root_sims, pv_plies=pv_plies)
        if isinstance(out, dict) and out.get("error"):
            err_str = str(out["error"]).lower()
            code = 400 if ("illegal" in err_str or "bad" in err_str or "terminal" in err_str) else 200
            return jsonify(out), code
        return jsonify(out)

    @app.route("/api/review_game", methods=["POST"])
    def api_review_game():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        pgn = data.get("pgn") or ""
        if not pgn.strip():
            return jsonify({"error": "pgn required"}), 400
        side = str(data.get("side") or "both").lower()
        if side not in ("both", "white", "black"):
            side = "both"
        sims_per_move = _coerce_int(data.get("sims_per_move"), 20)
        raw_mp = data.get("max_plies")
        if raw_mp is None or raw_mp == "":
            max_plies = None  # analyze full mainline (subject to server safety cap)
        else:
            max_plies = max(1, _coerce_int(raw_mp, 50_000))

        from webapp.game_reviewer import review_game

        out = review_game(
            pgn,
            side=side,
            sims_per_move=sims_per_move,
            max_plies=max_plies,
        )
        if isinstance(out, dict) and out.get("error"):
            return jsonify(out), 400
        return jsonify(out)

    @app.route("/api/pv", methods=["POST"])
    def api_pv():
        ok, err = _stockfish_ok()
        if not ok:
            return jsonify({"error": err or "Stockfish unavailable"}), 503
        data = request.get_json(force=True, silent=True) or {}
        fen = (data.get("fen") or "").strip()
        uci = (data.get("uci") or "").strip().lower()
        if not fen or not uci:
            return jsonify({"error": "fen and uci required"}), 400
        raw_tp = data.get("total_plies")
        if raw_tp is None:
            raw_tp = data.get("depth")
        total_plies = max(1, _coerce_int(raw_tp, 2))
        root_sims = _pv_root_sims_from_json(data)
        fallback_sims = _fallback_sims_from_json(data)
        try:
            Game().set_fen(fen)
        except Exception as e:
            return jsonify({"error": f"bad fen: {e}"}), 400

        out = svc.pv_line(
            fen,
            uci,
            max(1, total_plies),
            root_sims,
            fallback_sims,
        )
        return jsonify(out)

    return app


def apply_engine_until_human(sess: svc.PlaySession, depth: int, max_plies: int = 64) -> None:
    for _ in range(max_plies):
        g = sess.game
        done, _ = g.is_terminal()
        if done:
            return
        if g.turn == sess.human_color():
            return
        uci_move = svc.best_move_uci(g, depth, engine_elo=sess.engine_elo)
        if not uci_move:
            return
        mv = svc.parse_uci_legal(g, uci_move)
        if mv is None:
            return
        if not g.make_move(mv.start, mv.end, promotion=mv.promo):
            return
        sess.moves.append(uci_move)


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
