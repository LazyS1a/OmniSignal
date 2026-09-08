"""Deterministic, bounded candidate generation for exact and near duplicates."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from .processor import NormalizedDocument


@dataclass(frozen=True, slots=True)
class DuplicateCluster:
    group_id: str
    duplicate_kind: str
    normalized_ids: tuple[str, ...]


def cluster_duplicates(
    documents: tuple[NormalizedDocument, ...],
    *,
    normalization_run_id: str,
    max_distance: int,
) -> tuple[DuplicateCluster, ...]:
    eligible = tuple(
        sorted((doc for doc in documents if doc.exact_fingerprint), key=lambda doc: doc.normalized_id)
    )
    parent = {doc.normalized_id: doc.normalized_id for doc in eligible}
    by_id = {doc.normalized_id: doc for doc in eligible}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    exact_buckets: dict[str, list[str]] = defaultdict(list)
    lsh_buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for document in eligible:
        if document.exact_fingerprint:
            exact_buckets[document.exact_fingerprint].append(document.normalized_id)
        if document.simhash64:
            bits = int(document.simhash64, 16)
            for band in range(4):
                lsh_buckets[(band, (bits >> (band * 16)) & 0xFFFF)].append(document.normalized_id)
    for members in exact_buckets.values():
        for member in members[1:]:
            union(members[0], member)

    candidates: set[tuple[str, str]] = set()
    for members in lsh_buckets.values():
        ordered = sorted(set(members))
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                candidates.add((left, right))
    for left, right in sorted(candidates):
        left_bits = int(by_id[left].simhash64 or "0", 16)
        right_bits = int(by_id[right].simhash64 or "0", 16)
        if (left_bits ^ right_bits).bit_count() <= max_distance:
            union(left, right)

    components: dict[str, list[str]] = defaultdict(list)
    for normalized_id in sorted(parent):
        components[find(normalized_id)].append(normalized_id)
    clusters: list[DuplicateCluster] = []
    for members in sorted((tuple(sorted(values)) for values in components.values() if len(values) > 1)):
        fingerprints = {by_id[member].exact_fingerprint for member in members}
        duplicate_kind = "exact" if len(fingerprints) == 1 else "near"
        group_id = hashlib.sha256(
            (normalization_run_id + "\x1f" + "\x1f".join(members)).encode("utf-8")
        ).hexdigest()
        clusters.append(DuplicateCluster(group_id, duplicate_kind, members))
    return tuple(clusters)
