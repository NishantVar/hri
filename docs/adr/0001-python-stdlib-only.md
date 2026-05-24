# Python stdlib only — zero runtime dependencies

The daemon, CLI, renderer, and applier are stdlib-only Python. No Flask/FastAPI, no Jinja, no `jsonschema`, no `requests`. We pay in a more verbose `BaseHTTPRequestHandler`, hand-rolled HTML rendering, and a hand-rolled JSON validator, and we get back: `python3 scripts/riview.py daemon` runs anywhere `python3` runs, with no virtualenv and no `pip install` step.

This is a deliberate constraint, not laziness. Adding dependencies later is one-way: once a `requirements.txt` exists, the "just run it" property is gone.
