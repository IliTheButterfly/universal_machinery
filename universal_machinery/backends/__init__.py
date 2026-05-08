"""universal_machinery.backends -- vendor-specific PLC file format adapters.

Each backend is a separate package (typically a git submodule under
``backends/<vendor>/``) that implements the ``Backend`` protocol below.
Use a backend by importing from its package directly::

    from click_plc import decode_ckp           # read CKP
    from openplc_backend import OpenPlcBackend  # write PLCopen XML / ST

Or via the registry helper for backend-agnostic code::

    from universal_machinery.backends import get_backend
    b = get_backend("click")
    program = b.read("foo.ckp")

Backend authors: see ``base.py`` for the ``Backend`` ABC and the
``register()`` decorator.
"""
from .base import Backend, get_backend, register, registered_names

__all__ = ["Backend", "get_backend", "register", "registered_names"]
