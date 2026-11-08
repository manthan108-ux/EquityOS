"""LLM access (Phase 9): provider abstraction, JSON-only calls, safe errors.

The model reads and interprets; it never supplies numbers that are not on the page, and nothing it
returns enters the evidence store without passing the deterministic checks.
"""
