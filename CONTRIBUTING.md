# Contributing

1. Open an issue describing the user problem and expected behavior.
2. Create a focused branch. Install `requirements.txt` and `pytest` in a virtualenv.
3. Keep orchestration in the OpenClaw skill/config; keep prices, state and quotas
   in deterministic tools. New providers must normalize results and mock network
   traffic in tests.
4. Run `python -m pytest -q` and `python scripts/evaluate.py`.
5. Submit a PR with the behavior change, test evidence and any limitations.

Never commit keys, databases, phone numbers, booking confirmations, or raw chat
logs. Use synthetic examples. Do not add automatic payments or claim live
verification from mock fixtures. Contributions are licensed under MIT.
