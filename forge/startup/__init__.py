"""`forge start`: bring up the Space Monkey local-model stack for the Workbench.

The one public entry point is :func:`forge.startup.orchestrator.bring_up`.
Everything here is imported lazily from ``forge.cli.main`` so that
``forge check`` keeps importing neither execution nor network modules.
"""