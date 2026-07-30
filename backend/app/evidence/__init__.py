"""Clinical evidence layer — canonical schema, lifecycles, adapters and synthesis.

Read ``licensing`` and ``lifecycles`` before adding anything here. They encode two rules
that the rest of the layer assumes and that are expensive to discover late:

* What may be *stored* depends on the source's licence class, not on how the document
  was acquired. A reviewer uploading a paywalled PDF does not create a right to retain
  it.
* "Verified", "included in this network" and "this network is fit to compute on" are
  three independent lifecycles. Collapsing them into one flag makes a per-analysis
  judgement masquerade as a universal eligibility decision.
"""
