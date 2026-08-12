"""The autonomous loop: measure, decide, compose, ship.

Split in two on purpose. `plan` reads the open web and runs the model; `apply` holds the only
credential that can change getdailyvox.com. Nothing that reads untrusted text ever holds the
publishing token, which is the one property from the gated design worth keeping now that the human
gate is gone.
"""
