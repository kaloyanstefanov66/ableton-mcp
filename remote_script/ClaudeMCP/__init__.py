from __future__ import absolute_import, print_function, unicode_literals

try:
    from importlib import reload
except ImportError:  # Python 2 (Live 10): reload is a builtin
    pass


def create_instance(c_instance):
    # Reload on every instantiation: switching the Control Surface to None and back to
    # ClaudeMCP then picks up edited code without restarting Live.
    from . import compat, introspect, ClaudeMCP as module

    reload(compat)
    reload(introspect)
    module = reload(module)
    return module.ClaudeMCP(c_instance)
