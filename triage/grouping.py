"""Group near-duplicate posts so one assessment covers all of them."""
from __future__ import annotations

import re
from collections import defaultdict

_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"[@#]\w+")
_RT = re.compile(r"^\s*rt\s+", re.I)
_WORD = re.compile(r"[a-z0-9']+")
THRESHOLD = 0.6
SHINGLE = 3


def normalize(text: str) -> list[str]:
    t = _RT.sub("", (text or "").lower())
    t = _URL.sub(" ", t)
    t = _MENTION.sub(" ", t)
    return _WORD.findall(t)


def _shingles(tokens: list[str]) -> frozenset:
    if len(tokens) < SHINGLE:
        return frozenset([" ".join(tokens)]) if tokens else frozenset()
    return frozenset(" ".join(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1))


def group_indices(texts: list[str], links: list[str] | None = None) -> list[list[int]]:
    """Cluster texts whose word-triple sets overlap by at least THRESHOLD (Jaccard).

    Returns lists of indices into `texts`, in order of first appearance. Posts with the same
    link join one group. Texts under four words only group on an exact match.
    """
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
    index: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(sets):
        cands: dict[int, int] = defaultdict(int)
        for sh in s:
            for j in index[sh]:
                cands[j] += 1
        short_i = len(toks[i]) < 4
        for j, shared in cands.items():
            if find(i) == find(j):
                continue
            if short_i or len(toks[j]) < 4:
                if toks[i] == toks[j]:
                    union(i, j)
                continue
            if shared / (len(s) + len(sets[j]) - shared) >= THRESHOLD:
                union(i, j)
        for sh in s:
            index[sh].append(i)
    if links:
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
    return sorted(groups.values(), key=lambda g: g[0])
