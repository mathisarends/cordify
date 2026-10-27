from importlib import import_module


def exported(name, exports, namespace):
    if name not in exports:
        raise AttributeError(f"module {namespace['__name__']!r} has no attribute {name!r}")
    module, attribute = exports[name]
    value = getattr(import_module(module), attribute)
    namespace[name] = value
    return value
