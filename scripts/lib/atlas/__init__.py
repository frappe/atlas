# Remote-host execution package, shipped per-Task by script_uploads.py and
# imported on the server as `import atlas`. STDLIB ONLY — this code runs on a
# stock Ubuntu 24.04 droplet with no pip install, no Frappe, no Atlas app.
# That constraint is load-bearing: it is why these modules import and unit-test
# on the Atlas host (or anywhere) with no site and no droplet — the payoff the
# shell libraries could never offer.
#
# This package is the Python successor to scripts/lib/*.sh. The split mirrors
# the old one: pure name/path derivation and command-planning live in testable
# functions; the only thing that touches the host is run() in _run.py.
