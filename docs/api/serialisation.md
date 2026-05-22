# `universal_machinery.serialisation`

Canonical IL JSON.  `from_json(to_json(p)) == p` for any Program — pinned by `tests/test_public_api_surface.py::test_serialisation_round_trip_preserves_program`.

::: universal_machinery.serialisation
    options:
      members:
        - to_json
        - from_json
        - to_dict
        - from_dict
        - SerialisationError
