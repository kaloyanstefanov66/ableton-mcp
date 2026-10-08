import importlib


def create_instance(c_instance):
    # Reload on every instantiation: switching the Control Surface to None and back to
    # ClaudeMCP then picks up edited code without restarting Live.
    from . import introspect, ClaudeMCP as module

    importlib.reload(introspect)
    module = importlib.reload(module)
    return module.ClaudeMCP(c_instance)
