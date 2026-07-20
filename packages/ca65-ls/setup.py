"""Build the vendored tree-sitter-ca65 grammar as ca65_ls._grammar._binding.

Metadata lives in pyproject.toml; this file only defines the C extension.
Adapted from upstream tree-sitter-ca65's own setup.py (MIT, see
vendor/tree-sitter-ca65/NOTICE.md). The extension targets the CPython
limited API (abi3), so one wheel per platform covers Python 3.10+.
"""

from os import path
from platform import system
from sysconfig import get_config_var

from setuptools import Extension, setup

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:  # setuptools < 70
    from wheel.bdist_wheel import bdist_wheel

VENDOR = "vendor/tree-sitter-ca65"

sources = [
    f"{VENDOR}/bindings/python/binding.c",
    f"{VENDOR}/src/parser.c",
]
if path.exists(f"{VENDOR}/src/scanner.c"):
    sources.append(f"{VENDOR}/src/scanner.c")

macros: list[tuple[str, str | None]] = [
    ("PY_SSIZE_T_CLEAN", None),
    ("TREE_SITTER_HIDE_SYMBOLS", None),
]
if limited_api := not get_config_var("Py_GIL_DISABLED"):
    macros.append(("Py_LIMITED_API", "0x030A0000"))

cflags = ["-std=c11", "-fvisibility=hidden"] if system() != "Windows" else ["/std:c11", "/utf-8"]


class BdistWheel(bdist_wheel):
    def get_tag(self):
        python, abi, platform = super().get_tag()
        if python.startswith("cp") and limited_api:
            python, abi = "cp310", "abi3"
        return python, abi, platform


setup(
    ext_modules=[
        Extension(
            name="ca65_ls._grammar._binding",
            sources=sources,
            extra_compile_args=cflags,
            define_macros=macros,
            include_dirs=[f"{VENDOR}/src"],
            py_limited_api=limited_api,
        )
    ],
    cmdclass={"bdist_wheel": BdistWheel},
)
