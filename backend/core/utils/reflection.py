"""Dynamic import utilities - resolve classes/variables from dotted paths."""

from importlib import import_module


def resolve_variable(variable_path: str, expected_type=None):
    """Resolve a variable from a 'module.path:variable_name' string."""
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(
            f"{variable_path} doesn't look like a variable path. "
            "Example: package.module:VariableName"
        ) from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        raise ImportError(f"Could not import module {module_path}: {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(
            f"Module {module_path} does not define '{variable_name}'"
        ) from err

    if expected_type is not None and not isinstance(variable, expected_type):
        raise ValueError(
            f"{variable_path} is not an instance of {expected_type}, "
            f"got {type(variable).__name__}"
        )
    return variable


def resolve_class(class_path: str, base_class=None):
    """Resolve a class from 'module.path:ClassName'."""
    model_class = resolve_variable(class_path, expected_type=type)
    if not isinstance(model_class, type):
        raise ValueError(f"{class_path} is not a valid class")
    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(
            f"{class_path} is not a subclass of {base_class.__name__}"
        )
    return model_class
