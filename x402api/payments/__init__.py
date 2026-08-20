"""Server-side implementation of the x402 payment protocol.

types.py        wire shapes for v1 and v2, and the base64 header codec
facilitator.py  the verify/settle client, its CDP auth, and the test double
gate.py         framework-free decision logic (402 vs verify vs settle)
middleware.py   the thin Starlette/FastAPI adapter around gate.py
"""
