"""Checks the embedding model baked into an image, with no network.

``python -m hailhq.core.knowledge.selftest`` loads the model from
``hail_embedding_model_dir``, cuts an invented English manual into pieces,
and asks questions about it in Italian, German, Japanese and English: each
must find its section through the in-memory hybrid search. Run by both
Dockerfiles at build time (``--network=none``) and by CI's docker job. It
prints the load time and the query time; it exits non-zero on any miss.
"""

from __future__ import annotations

import sys
import time
import uuid

from hailhq.core.config import settings
from hailhq.core.knowledge.embed import Embedder
from hailhq.core.knowledge.index import MemoryIndex, Piece
from hailhq.core.knowledge.text import Block, chunk_blocks

# Invented product and company. Each section is its own piece.
SECTIONS = {
    "install": (
        "Installation",
        (
            "Mount the NS-5000 inverter on a vertical wall away from direct "
            "sunlight. Keep 30 cm of free space on every side for cooling. Use the "
            "wall bracket NS-WB-02 and four M8 screws."
        ),
    ),
    "error": (
        "Error codes",
        (
            "Error E-204 means the grid voltage is out of range. Check the AC "
            "breaker and wait five minutes; the inverter reconnects by itself. If "
            "E-204 stays on, call your installer."
        ),
    ),
    "battery": (
        "Battery",
        (
            "The NS-BAT-48V-100 battery stores 4.8 kWh. Charge it fully before "
            "first use and keep it between 5 and 40 degrees Celsius."
        ),
    ),
    "warranty": (
        "Warranty",
        (
            "The inverter has a ten year warranty from the date of purchase. "
            "Register the serial number online within 90 days to keep it."
        ),
    ),
    "wifi": (
        "Wi-Fi setup",
        (
            "Hold the Wi-Fi button for three seconds until the light blinks blue, "
            "then open the Northgate app and pick your home network."
        ),
    ),
}
QUESTIONS = [
    ("Quanto dura la garanzia dell'inverter?", "warranty"),
    ("Cosa significa l'errore E 204?", "error"),
    ("Come collego l'inverter al wifi di casa?", "wifi"),
    ("Wie viel Energie speichert die Batterie?", "battery"),
    ("インバーターはどこに取り付けますか？", "install"),
    ("what does ns bat 48 v 100 store", "battery"),
]


def run(model_dir: str) -> int:
    started = time.perf_counter()
    embedder = Embedder(model_dir)
    loaded = time.perf_counter() - started
    pieces, keys = [], []
    for key, (heading, body) in SECTIONS.items():
        chunk = chunk_blocks(
            [Block(heading, heading=True), Block(body)], embedder.count_tokens
        )[0]
        pieces.append(Piece(uuid.uuid4(), uuid.uuid4(), "manual.pdf", chunk.text))
        keys.append(key)
    index = MemoryIndex(pieces, embedder.embed_passages([p.text for p in pieces]))
    by_id = dict(zip((p.id for p in pieces), keys, strict=True))
    misses, times = 0, []
    for question, want in QUESTIONS:
        t = time.perf_counter()
        hits = index.search(question, embedder.embed_query(question), 3)
        times.append(time.perf_counter() - t)
        got = by_id[hits[0].piece.id] if hits else None
        if got != want:
            misses += 1
            print(f"miss: {question!r} gave {got}, want {want}")
    times.sort()
    print(
        f"knowledge selftest: load {loaded:.2f} s, query median "
        f"{times[len(times) // 2] * 1000:.1f} ms, {misses} miss(es)"
    )
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(
        run(sys.argv[1] if len(sys.argv) > 1 else settings.hail_embedding_model_dir)
    )
