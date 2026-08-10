"""Parsec source package.

Optional cap-evolve hook: when ``PARSEC_TOOLS_DIR`` names an existing directory,
every ``<name>.py`` file in it overrides the corresponding ``src.tools.<name>``
module for the lifetime of this process. Files whose name starts with ``_`` are
skipped. Used by cap-evolve (skillberry-ai/cap-evolve) to inject candidate tool
implementations into a running container without rebuilding the image. Unset in
production; the hook is then a no-op.
"""


def _load_tool_overrides() -> None:
    import importlib.util
    import os
    import sys

    override_dir = os.environ.get("PARSEC_TOOLS_DIR", "")
    if not override_dir or not os.path.isdir(override_dir):
        return

    for entry in sorted(os.listdir(override_dir)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        path = os.path.join(override_dir, entry)
        if not os.path.isfile(path):
            continue
        name = f"src.tools.{entry[:-3]}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)


_load_tool_overrides()
