"""Self-validating benchmark for VeriGate.

Deterministic synthetic corpus + answers (``bench/generate.py``) verified by
the real pipeline (``bench/run.py``) to publish two numbers: detection rate
and false-positive rate. No LLM, no network, one seeded ``random.Random``.
"""
