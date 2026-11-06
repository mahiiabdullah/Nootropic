"""Memory: what Neurovex knows about you, kept locally and recalled without AI calls.

- Writing piggybacks on chat: each chat step may carry add/update/forget operations,
  so remembering costs zero extra LLM calls.
- Recall uses SQLite FTS5 (BM25) over memories and past conversations: milliseconds,
  no tokens, and only a small, budgeted block is put in front of the model.
"""
