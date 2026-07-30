"""Coverage-driven question curation.

Every existing ingestion source is opportunistic: harvest returns what the web happened
to publish, social listening what people happened to post, prompt volume what was in the
uploaded CSV. None of them can answer "is there a question in the bank comparing Rinvoq
to Tremfya in psoriatic arthritis?", so the answer stayed no.

This package enumerates the comparisons that SHOULD exist from the taxonomy, subtracts
the ones that already do, and asks the model to write only the difference. Generated
candidates stage into the same reviewer queue as harvested ones and clear the same
Medical-Affairs gate — nothing here shortens the path to a monitoring run.
"""
