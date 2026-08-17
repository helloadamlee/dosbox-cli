"""Holds a copy of scripts/host_control_client.py injected at packaging time.

Empty in a development checkout — _client_import falls back to the sibling
scripts/ directory. The bundle assembly step (scripts/assemble_bundle.py)
copies the real module in here so a plain `pip install` works.
"""
