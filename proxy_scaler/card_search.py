"""Card-name search over the local corpus, for the Decklist's Quick Add and
Card Search: forgiving enough that "so rin" finds Sol Ring, "lig bol"
finds Lightning Bolt and "sol rnig" still finds Sol Ring.

Names are matched in memory, not in SQL: one entry per card (oracle_id),
under its English name, each face of a double-faced name, and its printed
names in the language asked for. The index takes a second or so to build
from a full corpus. The first search builds it (warm() can start that in
the background, once a UI searches); it is rebuilt when the corpus is
reimported (its imported_at meta changes).

Ranking is by how well a name matches (see _tier), then by how many
English printings the card has, which stands in for popularity (the corpus
keeps no EDHREC rank): "sol" puts Sol Ring above Solemn Simulacrum.
Tokens, emblems and digital-only cards sort after real paper cards, and
art-series cards are left out entirely.
"""

from __future__ import annotations

import bisect
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import carddb
from .mpc_catalog import search_key

# Tiers, best first. A card's tier is the best of its names' tiers.
EXACT, PREFIX, WORDS_IN_ORDER, WORDS_ANY_ORDER, SUBSTRING = range(5)

_NON_WORD = re.compile(r"[^a-z0-9]+")

# Sorted after real cards (carddb._NON_GAME_LAYOUT_SQL, less art_series,
# which is left out: an art card isn't a card anyone proxies by name).
_LESSER_LAYOUTS = frozenset(
    {"token", "double_faced_token", "emblem", "front_card", "vanguard", "planar", "scheme"}
)
_SKIPPED_LAYOUTS = frozenset({"art_series"})


def name_key(text: str) -> str:
    """Lower case, accents and apostrophes folded (mpc_catalog.search_key),
    and any other punctuation a space: "Lim-Dûl's Vault" is "lim duls
    vault", "Borborygmos, Enraged" is "borborygmos enraged"."""
    return " ".join(_NON_WORD.sub(" ", search_key(text)).split())


@dataclass
class _Name:
    text: str
    compact: str
    words: list[str]


@dataclass
class _Entry:
    oracle_id: str
    name: str
    printings: int
    # Sorts last: a non-game layout, or no paper printing at all.
    lesser: bool
    names: list[_Name] = field(default_factory=list)


@dataclass
class _Index:
    imported_at: str
    entries: list[_Entry]
    by_oracle: dict[str, _Entry]
    # Every word of every name, sorted, for correcting a mistyped word.
    vocabulary: list[str] = field(default_factory=list)
    # Languages whose printed names have been added.
    langs: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class SearchHit:
    oracle_id: str
    name: str
    printings: int


_lock = threading.Lock()
_indexes: dict[str, _Index] = {}


def _cache_key(db_path: Path | str | None) -> str:
    return str(db_path or carddb.DEFAULT_CARD_DB_PATH)


def _imported_at(conn: sqlite3.Connection) -> str:
    return carddb.get_meta(conn).get(carddb.META_IMPORTED_AT, "")


def _add_name(entry: _Entry, name: str) -> None:
    parts = [name] + (name.split(" // ") if " // " in name else [])
    for part in parts:
        text = name_key(part)
        if text and all(n.text != text for n in entry.names):
            entry.names.append(_Name(text, text.replace(" ", ""), text.split()))


def _build(conn: sqlite3.Connection, imported_at: str) -> _Index:
    # One pass over the narrow columns, aggregated here: a GROUP BY over a
    # full (every language) corpus is several times slower.
    entries: list[_Entry] = []
    by_oracle: dict[str, _Entry] = {}
    paper: set[str] = set()
    for oracle_id, name, layout, digital, english in conn.execute(
        "SELECT oracle_id, name, layout, digital, lang = 'en' FROM cards"
        " WHERE oracle_id IS NOT NULL"
    ):
        if layout in _SKIPPED_LAYOUTS:
            continue
        entry = by_oracle.get(oracle_id)
        if entry is None:
            entry = _Entry(oracle_id, name, 0, layout in _LESSER_LAYOUTS)
            _add_name(entry, name)
            by_oracle[oracle_id] = entry
            entries.append(entry)
        elif name != entry.name:
            _add_name(entry, name)
        entry.printings += english
        if not digital:
            paper.add(oracle_id)
    for entry in entries:
        if entry.oracle_id not in paper:
            entry.lesser = True
    index = _Index(imported_at, entries, by_oracle)
    _refresh_vocabulary(index)
    return index


def _refresh_vocabulary(index: _Index) -> None:
    index.vocabulary = sorted({w for e in index.entries for n in e.names for w in n.words})


def _add_lang(conn: sqlite3.Connection, index: _Index, lang: str) -> None:
    """Printed names in `lang` as more names to match, so a German name
    finds its card. English names are always there."""
    if lang == "en" or lang in index.langs:
        return
    for oracle_id, printed_name in conn.execute(
        "SELECT DISTINCT oracle_id, printed_name FROM cards"
        " WHERE lang = ? AND printed_name IS NOT NULL AND oracle_id IS NOT NULL",
        (lang,),
    ):
        entry = index.by_oracle.get(oracle_id)
        if entry is not None:
            _add_name(entry, printed_name)
    index.langs.add(lang)
    _refresh_vocabulary(index)


def _index_for(conn: sqlite3.Connection, db_path: Path | str | None, lang: str) -> _Index:
    imported_at = _imported_at(conn)
    key = _cache_key(db_path)
    with _lock:
        index = _indexes.get(key)
        if index is None or index.imported_at != imported_at:
            index = _build(conn, imported_at)
            _indexes[key] = index
        _add_lang(conn, index, lang)
        return index


def warm(db_path: Path | str | None) -> None:
    """Build the index in the background if it isn't built for the current
    corpus, so the first search doesn't wait. Never raises."""

    def run() -> None:
        conn = carddb.open_if_ready(db_path)
        if conn is None:
            return
        try:
            _index_for(conn, db_path, "en")
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    with _lock:
        if _cache_key(db_path) in _indexes:
            return
    threading.Thread(target=run, name="card-search-warm", daemon=True).start()


# ---------------------------------------------------------------------------
# Matching


def _words_in_order(words: list[str], tokens: list[tuple[str, ...]]) -> bool:
    i = 0
    for word in words:
        if i < len(tokens) and word.startswith(tokens[i]):
            i += 1
    return i == len(tokens)


def _tier(name: _Name, query: str, tokens: list[tuple[str, ...]]) -> int | None:
    """How `name` matches. Each token is a tuple of acceptable spellings
    (one, unless a typo was corrected)."""
    if name.text == query:
        return EXACT
    # Typed with no spaces, "solring" is a prefix of "sol ring". With
    # spaces the words count ("so rin" is Sol Ring, not Sorin).
    if name.text.startswith(query) or (" " not in query and name.compact.startswith(query)):
        return PREFIX
    if not all(any(s in name.text for s in t) for t in tokens):
        return None
    if _words_in_order(name.words, tokens):
        return WORDS_IN_ORDER
    if all(any(w.startswith(t) for w in name.words) for t in tokens):
        return WORDS_ANY_ORDER
    return SUBSTRING


def _rank(index: _Index, query: str, tokens: list[tuple[str, ...]]) -> list[_Entry]:
    scored = []
    for entry in index.entries:
        best = min(
            (t for t in (_tier(n, query, tokens) for n in entry.names) if t is not None),
            default=None,
        )
        if best is not None:
            scored.append(((entry.lesser, best, -entry.printings, len(entry.name), entry.name), entry))
    scored.sort(key=lambda pair: pair[0])
    return [entry for _, entry in scored]


def _within_one_edit(a: str, b: str) -> bool:
    """Equal-length or off-by-one strings one substitution, insertion,
    deletion or adjacent swap apart."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    while i < min(la, lb) and a[i] == b[i]:
        i += 1
    if la == lb:
        if a[i + 1 :] == b[i + 1 :]:
            return True  # substitution
        return (
            i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2 :] == b[i + 2 :]
        )  # adjacent swap
    longer, shorter = (a, b) if la > lb else (b, a)
    return longer[i + 1 :] == shorter[i:]


def _spellings(index: _Index, token: str) -> tuple[str, ...]:
    """`token` plus, when no name word starts with it, the word prefixes
    one edit away from it ("rnig" → "ring")."""
    vocab = index.vocabulary
    at = bisect.bisect_left(vocab, token)
    if len(token) < 3 or (at < len(vocab) and vocab[at].startswith(token)):
        return (token,)
    fixes: set[str] = set()
    for word in vocab:
        for size in (len(token) - 1, len(token), len(token) + 1):
            prefix = word[:size]
            if len(prefix) == size and _within_one_edit(token, prefix):
                fixes.add(prefix)
    return (token, *sorted(fixes))


def search(
    conn: sqlite3.Connection,
    db_path: Path | str | None,
    query: str,
    *,
    lang: str = "en",
    limit: int = 8,
) -> list[SearchHit]:
    """The best `limit` cards for a typed query; nothing for an empty one.
    Hits on what was typed come first, then hits on a corrected spelling."""
    q = name_key(query)
    if not q:
        return []
    index = _index_for(conn, db_path, lang)
    words = q.split()
    ranked = _rank(index, q, [(w,) for w in words])
    if len(ranked) < limit:
        spellings = [_spellings(index, w) for w in words]
        if any(len(s) > 1 for s in spellings):
            seen = {e.oracle_id for e in ranked}
            ranked += [e for e in _rank(index, q, spellings) if e.oracle_id not in seen]
    return [SearchHit(e.oracle_id, e.name, e.printings) for e in ranked[:limit]]


def clear_cache() -> None:
    """Tests only: forget every built index."""
    with _lock:
        _indexes.clear()
