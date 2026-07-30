"""Computation engines for Level 3 of the evidence hierarchy (Phase 6).

Three engines, chosen by the protocol's ``model_selection_rule`` against the facts in
``evidence.topology`` — never by whichever is convenient:

    pairwise   direct head-to-head pooling; also the input to Bucher
    bucher     adjusted indirect comparison through a common comparator
    netmeta    full network meta-analysis, via the R sidecar

``pairwise`` and ``bucher`` are pure Python and exact, so they are computed in-process and
their tests run offline. ``netmeta`` is a wire contract to an R sidecar, because
reimplementing a validated graph-theoretic NMA would mean asking reviewers to trust our
arithmetic over ``netmeta``'s.
"""
