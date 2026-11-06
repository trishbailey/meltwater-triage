"""Group near-duplicate posts so one assessment covers all of them."""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"[@#]\w+")
_RT = re.compile(r"^\s*rt\s+", re.I)
_WORD = re.compile(r"[a-z0-9']+")
THRESHOLD = 0.6
SHINGLE = 3
TOKEN_THRESHOLD = 0.8


def normalize(text: str) -> list[str]:
    t = _RT.sub("", (text or "").lower())
    t = _URL.sub(" ", t)
    t = _MENTION.sub(" ", t)
    return _WORD.findall(t)


def _shingles(tokens: list[str]) -> frozenset:
    if len(tokens) < SHINGLE:
        return frozenset([" ".join(tokens)]) if tokens else frozenset()
    return frozenset(" ".join(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1))


def _jaccard(sa: frozenset, sb: frozenset) -> float:
    """Jaccard of two word sets; catches a reworded or lightly edited copy of the same post."""
    if min(len(sa), len(sb)) < 5:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


MAX_BUCKET = 80  # compare a new post with at most this many earlier posts per shared phrase


@lru_cache(maxsize=8)
def _group_cached(texts: tuple, links: tuple) -> tuple:
    n = len(texts)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    toks = [normalize(t) for t in texts]
    sets = [_shingles(t) for t in toks]
    words = [frozenset(t) for t in toks]
    exact: dict[tuple, int] = {}
    index: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(sets):
        key = tuple(toks[i])
        if key and key in exact:  # identical wording after cleanup
            union(i, exact[key])
            for sh in s:
                if len(index[sh]) < MAX_BUCKET:
                    index[sh].append(i)
            continue
        if key:
            exact[key] = i
        cands: dict[int, int] = defaultdict(int)
        for sh in s:
            for j in index[sh]:
                cands[j] += 1
        short_i = len(toks[i]) < 4
        for j, shared in cands.items():
            if find(i) == find(j) or short_i or len(toks[j]) < 4:
                continue
            if shared / (len(s) + len(sets[j]) - shared) >= THRESHOLD or _jaccard(words[i], words[j]) >= TOKEN_THRESHOLD:
                union(i, j)
        for sh in s:
            if len(index[sh]) < MAX_BUCKET:
                index[sh].append(i)
    seen: dict[str, int] = {}
    for i, l in enumerate(links):
        if l:
            if l in seen:
                union(i, seen[l])
            else:
                seen[l] = i
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return tuple(tuple(g) for g in sorted(groups.values(), key=lambda g: g[0]))


def group_indices(texts: list[str], links: list[str] | None = None) -> list[list[int]]:
    """Cluster near-duplicate texts (word-triple Jaccard >= THRESHOLD, or word-set Jaccard >= TOKEN_THRESHOLD).

    Returns lists of indices into `texts`, in order of first appearance. Posts with the same
    link join one group. Texts under four words only group on an exact match.
    """
    links = list(links) if links else [""] * len(texts)
    return [list(g) for g in _group_cached(tuple(texts), tuple(links))]
