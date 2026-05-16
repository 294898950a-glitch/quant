"""Helpers for module-object aliases."""

from importlib import import_module
import sys


def alias_module(alias: str, target: str):
    module = import_module(target)
    sys.modules[alias] = module
    return module
