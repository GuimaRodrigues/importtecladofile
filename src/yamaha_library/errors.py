"""Domain-specific exceptions with messages suitable for CLI and GUI users."""


class YamahaLibraryError(Exception):
    """Base class for expected application errors."""


class FormatError(YamahaLibraryError):
    """Raised when a source file is malformed, truncated, or inconsistent."""


class UnsupportedFormatError(FormatError):
    """Raised when the file family or version cannot be handled safely."""


class DependencyError(YamahaLibraryError):
    """Raised for missing, ambiguous, or unsafe dependencies."""


class ExportError(YamahaLibraryError):
    """Raised when an output cannot be produced safely."""

