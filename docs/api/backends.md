# `universal_machinery.backends`

Backend ABC + registry.  Backends live in separate submodule packages (`openplc_backend`, `rusty_backend`, `click_plc`) and register themselves via the `@register(name)` decorator on import.

::: universal_machinery.backends
    options:
      members:
        - Backend
        - UnsupportedOpError
        - register
        - get_backend
        - registered_names
