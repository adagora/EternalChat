"""Binary summary-tree addressing and the merge choice. Pure functions, no IO.

Node (l, i) covers 2**l messages starting at i * 2**l and is written id+n.
"""
from fractions import Fraction


def address(key):
    """(l, i) -> (first message id, message count)."""
    l, i = key
    return i * (1 << l), 1 << l


def key_for(start, count):
    """(id, n) -> (l, i); n must be a power of two and id a multiple of n."""
    if type(start) is not int or type(count) is not int:
        raise ValueError("id and n must be integers")
    if start < 0 or count < 1 or count & (count - 1) or start % count:
        raise ValueError("n must be a power of two and id a nonnegative multiple of n")
    return count.bit_length() - 1, start // count


def children(key):
    l, i = key
    return (l - 1, 2 * i), (l - 1, 2 * i + 1)


def parent(key):
    l, i = key
    return l + 1, i // 2


def sibling(key):
    l, i = key
    return l, i ^ 1


def candidate(nodes, total, available):
    """Choose the adjacent sibling pair to merge next, or None.

    Highest (total - last message) / child size wins, oldest first on ties, and
    only pairs whose parent is in `available`. This reproduces the rollback
    order of the UniiChat design. Returns (index of the left child, parent).
    """
    best = None
    for j in range(len(nodes) - 1):
        l, i = nodes[j]
        if i % 2 or nodes[j + 1] != (l, i + 1):
            continue
        merged = parent(nodes[j])
        if merged not in available:
            continue
        first, size = address(nodes[j])
        last = first + size * 2 - 1
        score = (Fraction(total - last, size), -first)
        if best is None or score > best[0]:
            best = score, j, merged
    return None if best is None else (best[1], best[2])
