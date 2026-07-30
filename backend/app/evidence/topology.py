"""Treatment-network topology (Phase 0 audit + Phase 6 method selection).

One implementation, two consumers. The Phase 0 feasibility audit asks *"is this
indication's network even connected?"*; the Phase 6 resolver asks *"Bucher or
netmeta?"*. Both are questions about the same graph, and two implementations would
eventually disagree about whether a network has a closed loop — which would mean the
audit promised a comparison the resolver then refused to compute.

Nodes are treatments; an edge exists between two treatments randomised in the same
study. Edge weight is the number of studies contributing that comparison.

**A caveat worth stating plainly.** ``loop_count`` is the cyclomatic number, which counts
every cycle in the aggregate graph. A single three-arm trial forms a triangle, but that
triangle is *not* independent evidence — the three comparisons share a control group and
are correlated. For choosing an engine that distinction does not matter (a multi-arm
trial sends you to ``netmeta`` anyway), but for **inconsistency assessment it matters a
great deal**, so ``independent_loop_count`` excludes cycles that live inside one study.
Do not use ``loop_count`` to claim a network can be tested for inconsistency.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Topology:
    """The shape of an evidence network, derived from its studies' arms."""

    nodes: tuple[str, ...]
    # (treatment_a, treatment_b, study_count) with a < b, so an edge has one spelling.
    edges: tuple[tuple[str, str, int], ...]
    multi_arm_studies: tuple[str, ...]
    components: tuple[frozenset[str], ...]
    study_arms: Mapping[str, frozenset[str]]

    # --- connectivity -----------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """True when every treatment is reachable from every other."""
        return len(self.components) <= 1 and bool(self.nodes)

    def component_of(self, treatment: str) -> frozenset[str]:
        for component in self.components:
            if treatment in component:
                return component
        return frozenset()

    def are_connected(self, a: str, b: str) -> bool:
        """True when a path of shared comparators links *a* and *b*.

        This is the precondition for any indirect estimate at all. When it is False the
        honest answer is ``NETWORK_DISCONNECTED`` — a structured evidence gap, not a
        number.
        """
        return a != b and a in self.component_of(b)

    def path(self, a: str, b: str) -> tuple[str, ...]:
        """Shortest path of treatments from *a* to *b*, or ``()`` when disconnected.

        Shortest matters: each additional link compounds the transitivity assumption, so
        a two-hop indirect comparison is a weaker claim than a one-hop one.
        """
        if a == b or a not in self._adjacency or b not in self._adjacency:
            return ()
        queue: list[tuple[str, ...]] = [(a,)]
        seen = {a}
        while queue:
            route = queue.pop(0)
            for neighbour in sorted(self._adjacency[route[-1]]):
                if neighbour == b:
                    return (*route, b)
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((*route, neighbour))
        return ()

    # --- comparator structure ----------------------------------------------------------
    def neighbours(self, treatment: str) -> frozenset[str]:
        return self._adjacency.get(treatment, frozenset())

    def shared_comparators(self, a: str, b: str) -> tuple[str, ...]:
        """Treatments compared directly against BOTH *a* and *b*.

        These are the anchors an adjusted indirect comparison runs through. Phase 0
        audits this per indication because without at least one, a cross-class comparison
        is not estimable regardless of how good the underlying trials are.
        """
        return tuple(sorted(self.neighbours(a) & self.neighbours(b)))

    def has_direct_evidence(self, a: str, b: str) -> bool:
        return b in self.neighbours(a)

    # --- loops and multi-arm ------------------------------------------------------------
    @property
    def loop_count(self) -> int:
        """Cyclomatic number: ``edges - nodes + components``.

        Counts within-study triangles. See the module docstring — use
        ``independent_loop_count`` for anything inconsistency-related.
        """
        if not self.nodes:
            return 0
        return len(self.edges) - len(self.nodes) + len(self.components)

    @property
    def has_closed_loops(self) -> bool:
        return self.loop_count > 0

    @property
    def independent_loop_count(self) -> int:
        """Loops that survive removing every within-study clique.

        Rebuilt from edges that appear in more than one study, or that connect
        treatments never randomised together in a single multi-arm trial. This is the
        number that says whether inconsistency can be assessed at all.
        """
        cross_study_edges = [
            (a, b) for a, b, _count in self.edges if not self._is_within_single_study(a, b)
        ]
        if not cross_study_edges:
            return 0
        nodes = {n for edge in cross_study_edges for n in edge}
        components = _components(nodes, cross_study_edges)
        return max(0, len(cross_study_edges) - len(nodes) + len(components))

    @property
    def has_multi_arm_studies(self) -> bool:
        return bool(self.multi_arm_studies)

    def _is_within_single_study(self, a: str, b: str) -> bool:
        """True when this comparison comes only from one multi-arm study."""
        contributors = [s for s, arms in self.study_arms.items() if a in arms and b in arms]
        return len(contributors) == 1 and len(self.study_arms[contributors[0]]) > 2

    # --- engine selection input ---------------------------------------------------------
    @property
    def is_simple_star(self) -> bool:
        """True for a single common comparator, no closed loops, no multi-arm trials.

        The one topology where Bucher and ``netmeta`` coincide under matched assumptions,
        and therefore the only one where the cheaper engine is defensible. The actual
        selection rule lives in ``AnalysisProtocolDefinition.model_selection_rule``, not
        here — this only supplies the facts it decides on.
        """
        if not self.is_connected or self.has_closed_loops or self.has_multi_arm_studies:
            return False
        centres = [n for n in self.nodes if len(self.neighbours(n)) == len(self.nodes) - 1]
        return len(self.nodes) > 2 and len(centres) == 1

    @property
    def _adjacency(self) -> dict[str, frozenset[str]]:
        return _adjacency_of(self.edges)

    def summary(self) -> dict[str, object]:
        """Flat description for the Phase 0 audit matrix and network records."""
        return {
            "nodes": list(self.nodes),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "study_count": len(self.study_arms),
            "is_connected": self.is_connected,
            "component_count": len(self.components),
            "largest_component": sorted(max(self.components, key=len)) if self.components else [],
            "loop_count": self.loop_count,
            "independent_loop_count": self.independent_loop_count,
            "has_multi_arm_studies": self.has_multi_arm_studies,
            "multi_arm_studies": list(self.multi_arm_studies),
            "is_simple_star": self.is_simple_star,
        }


def _adjacency_of(edges: Iterable[tuple[str, str, int]]) -> dict[str, frozenset[str]]:
    built: dict[str, set[str]] = {}
    for a, b, *_ in edges:
        built.setdefault(a, set()).add(b)
        built.setdefault(b, set()).add(a)
    return {k: frozenset(v) for k, v in built.items()}


def _components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> tuple[frozenset[str], ...]:
    adjacency: dict[str, set[str]] = {n: set() for n in nodes}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    seen: set[str] = set()
    found: list[frozenset[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack, group = [start], set()
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            stack.extend(adjacency[node] - group)
        seen |= group
        found.append(frozenset(group))
    return tuple(sorted(found, key=lambda c: (-len(c), sorted(c))))


def build(study_arms: Mapping[str, Sequence[str]]) -> Topology:
    """Build a topology from ``{study_id: [treatment, ...]}``.

    Duplicate treatments within a study (two doses of the same drug pooled to one node)
    collapse to a single arm, because a self-comparison is not an edge. Whether those
    doses *should* have been pooled is a ``dose_policy`` decision made before this point.
    """
    cleaned: dict[str, frozenset[str]] = {}
    for study_id, arms in study_arms.items():
        treatments = frozenset(t.strip() for t in arms if t and t.strip())
        if len(treatments) >= 2:
            cleaned[study_id] = treatments

    edge_counts: dict[tuple[str, str], int] = {}
    for treatments in cleaned.values():
        for a, b in combinations(sorted(treatments), 2):
            edge_counts[(a, b)] = edge_counts.get((a, b), 0) + 1

    nodes = tuple(sorted({t for arms in cleaned.values() for t in arms}))
    edges = tuple((a, b, count) for (a, b), count in sorted(edge_counts.items()))

    return Topology(
        nodes=nodes,
        edges=edges,
        multi_arm_studies=tuple(sorted(s for s, arms in cleaned.items() if len(arms) > 2)),
        components=_components(nodes, [(a, b) for a, b, _ in edges]),
        study_arms=cleaned,
    )
