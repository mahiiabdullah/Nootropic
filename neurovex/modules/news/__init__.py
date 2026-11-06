"""News module: discovers tech news from RSS/Atom feeds and summarizes it.

Pipeline: fetch feeds → dedupe → store as DISCOVERED → extract text →
summarize with an LLM (free cloud providers via neurovex.llm) → PROCESSED / SKIPPED / FAILED.
"""
