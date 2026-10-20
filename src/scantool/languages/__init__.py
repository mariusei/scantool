"""Unified language system for scantool.

This package provides a single interface for language support, combining
scanner functionality (structure extraction) and analyzer functionality
(semantic analysis) into one class per language.

Usage:
    from scantool.languages import get_language, LanguageRegistry

    # Get language handler by extension
    lang = get_language('.py')
    structures = lang.scan(source_code)
    imports = lang.extract_imports(file_path, content)

    # Get all registered languages
    registry = LanguageRegistry()
    for ext, lang_cls in registry.items():
        print(f"{ext}: {lang_cls.get_language_name()}")

Models are available from the models submodule:
    from scantool.languages.models import (
        StructureNode,
        ImportInfo,
        EntryPointInfo,
        DefinitionInfo,
        CallInfo,
    )
"""

import importlib
import os
import pkgutil
from typing import Optional

from .base import BaseLanguage
from .models import (
    CallGraphNode,
    CallInfo,
    CodeMapResult,
    DefinitionInfo,
    EntryPointInfo,
    FileNode,
    ImportInfo,
    StructureNode,
    is_file_info_stub,
)

__all__ = [
    # Core classes
    "BaseLanguage",
    "LanguageRegistry",
    "get_language",
    "get_language_for",
    "get_registry",
    "shebang_interpreter",
    # Models
    "StructureNode",
    "is_file_info_stub",
    "ImportInfo",
    "EntryPointInfo",
    "DefinitionInfo",
    "CallInfo",
    "CallGraphNode",
    "FileNode",
    "CodeMapResult",
]


class LanguageRegistry:
    """Registry of all language handlers.

    Provides lookup by file extension and auto-discovers language
    implementations in this package.
    """

    _instance: Optional["LanguageRegistry"] = None
    _languages: dict[str, type[BaseLanguage]]
    _interpreters: dict[str, type[BaseLanguage]]
    _filenames: dict[str, type[BaseLanguage]]
    _instances: dict[str, BaseLanguage]

    def __new__(cls):
        """Singleton pattern for registry."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._languages = {}
            cls._instance._interpreters = {}
            cls._instance._filenames = {}
            cls._instance._instances = {}
            cls._instance._discover_languages()
        return cls._instance

    def _discover_languages(self):
        """Auto-discover language implementations in this package."""
        # Import all modules in this package
        package_path = __path__
        for _importer, modname, _ispkg in pkgutil.iter_modules(package_path):
            if modname in ("base", "models", "__init__"):
                continue
            try:
                module = importlib.import_module(f".{modname}", __package__)
                # Find BaseLanguage subclasses
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseLanguage)
                        and attr is not BaseLanguage
                    ):
                        self.register(attr)
            except ImportError:
                pass  # Skip modules that fail to import

    def register(self, language_cls: type[BaseLanguage]):
        """Register a language handler.

        Args:
            language_cls: BaseLanguage subclass to register
        """
        for ext in language_cls.get_extensions():
            ext_lower = ext.lower()
            # Check priority if extension already registered
            if ext_lower in self._languages:
                existing = self._languages[ext_lower]
                if language_cls.get_priority() <= existing.get_priority():
                    continue
            self._languages[ext_lower] = language_cls
        for interpreter in language_cls.SHEBANG_INTERPRETERS:
            self._interpreters[interpreter] = language_cls
        for filename in language_cls.FILENAMES:
            self._filenames[filename.lower()] = language_cls

    def get_class_for(
        self, path: str, head: str | bytes | None = None
    ) -> type[BaseLanguage] | None:
        """The handler class for a file: by its extension; then by its name
        up to the first dot (`Makefile`, `Dockerfile.dev`; see
        BaseLanguage.FILENAMES); for a file without an extension, by the
        interpreter its `#!` line names (scripts in bin/ and libexec/).

        Args:
            path: The file's path; only its name is used, and the file is
                read for its first line when `head` is not given
            head: The start of the file's content, when the caller has it
                (a file read from a git ref, or content passed in)
        """
        name = os.path.basename(path)
        extension = os.path.splitext(name)[1].lower()
        if extension in self._languages:
            return self._languages[extension]
        by_name = self._filenames.get(name.split(".", 1)[0].lower())
        if by_name is not None or extension:
            return by_name
        if not self._interpreters:
            return None
        if head is None:
            try:
                with open(path, "rb") as f:
                    head = f.read(_SHEBANG_READ)
            except OSError:
                return None
        interpreter = shebang_interpreter(head)
        return self._interpreters.get(interpreter) if interpreter else None

    def get_class_for_interpreter(self, command: str) -> type[BaseLanguage] | None:
        """The handler class for the scripts an interpreter command runs
        (`python3`, `/usr/bin/ruby`), by SHEBANG_INTERPRETERS."""
        interpreter = shebang_interpreter("#!" + command)
        return self._interpreters.get(interpreter) if interpreter else None

    def get_for(self, path: str, head: str | bytes | None = None) -> BaseLanguage | None:
        """The handler instance for a file (see get_class_for)."""
        language_cls = self.get_class_for(path, head)
        if language_cls is None:
            return None
        key = language_cls.__qualname__
        if key not in self._instances:
            self._instances[key] = language_cls()
        return self._instances[key]

    def get(self, extension: str) -> BaseLanguage | None:
        """Get language handler instance for extension.

        Args:
            extension: File extension (e.g., '.py', '.ts')

        Returns:
            Language handler instance, or None if not found
        """
        ext_lower = extension.lower()
        if ext_lower not in self._languages:
            return None

        # Cache instances
        if ext_lower not in self._instances:
            self._instances[ext_lower] = self._languages[ext_lower]()

        return self._instances[ext_lower]

    def get_class(self, extension: str) -> type[BaseLanguage] | None:
        """Get language handler class for extension.

        Args:
            extension: File extension

        Returns:
            Language handler class, or None if not found
        """
        return self._languages.get(extension.lower())

    def items(self):
        """Iterate over (extension, class) pairs."""
        return self._languages.items()

    def extensions(self):
        """Get all registered extensions."""
        return self._languages.keys()

    def languages(self):
        """Get all registered language classes."""
        return set(self._languages.values())

    # Backward compatibility methods (match old ScannerRegistry/AnalyzerRegistry interface)

    def get_scanner(self, extension: str) -> type[BaseLanguage] | None:
        """Get language class for extension (backward compatible with ScannerRegistry)."""
        return self.get_class(extension)

    def get_analyzer(self, extension: str) -> type[BaseLanguage] | None:
        """Get language class for extension (backward compatible with AnalyzerRegistry)."""
        return self.get_class(extension)

    def get_supported_extensions(self) -> list[str]:
        """Get sorted list of all supported extensions."""
        return sorted(self._languages.keys())

    def get_scanner_info(self) -> dict[str, str]:
        """Get mapping of extensions to language names."""
        return {ext: cls.get_language_name() for ext, cls in self._languages.items()}


# Module-level convenience functions

_registry: LanguageRegistry | None = None


def get_registry() -> LanguageRegistry:
    """Get the global language registry."""
    global _registry
    if _registry is None:
        _registry = LanguageRegistry()
    return _registry


def get_language_for(path: str, head: str | bytes | None = None) -> BaseLanguage | None:
    """Language handler instance for a file, by extension or `#!` line
    (see LanguageRegistry.get_class_for)."""
    return get_registry().get_for(path, head)


# Enough of a file for any `#!` line; the kernel itself reads at most 256 bytes.
_SHEBANG_READ = 256


def shebang_interpreter(head: str | bytes) -> str | None:
    """The interpreter a `#!` line names, without path or version, or None:
    `#!/bin/bash -e` is bash, `#!/usr/bin/env python3.12` is python,
    `#!/usr/bin/env -S node --flag` is node."""
    if isinstance(head, bytes):
        head = head[:_SHEBANG_READ].decode("utf-8", "replace")
    if not head.startswith("#!"):
        return None
    words = head[2:].split("\n", 1)[0].split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    if not words:
        return None
    return os.path.basename(words[0]).rstrip("0123456789.") or None


def get_language(extension: str) -> BaseLanguage | None:
    """Get language handler instance for extension.

    Args:
        extension: File extension (e.g., '.py', '.ts')

    Returns:
        Language handler instance, or None if not found
    """
    return get_registry().get(extension)
