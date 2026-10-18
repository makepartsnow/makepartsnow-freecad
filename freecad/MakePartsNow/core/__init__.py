"""Make Parts Now CAD core: the host-free part of the CAD add-ins.

The ``/mcp/cad`` client, background jobs, the quote, revise, checkout and delete
flows, the units check, settings, consent text, usage counts, the panel's view
model and its controller. Standard library only, Python 3.11 or newer, and
package-relative imports only, so an add-in can vendor this folder under any
name (``scripts/sync-core.py``) without clashing with other add-ins in the CAD
program's shared interpreter. Nothing here imports a CAD program's API: the
host passes a ``host.Host`` (who it is) and an adapter (how to reach the model
and the panel).
"""

CORE_VERSION = "0.1.4"
