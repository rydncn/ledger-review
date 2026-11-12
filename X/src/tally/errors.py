"""Errors Tally reports to the user.

Messages must never contain statement contents (descriptions, amounts,
payees) unless the user explicitly asked to see them. Keep them about
files, lines, columns and configuration.
"""

# Exit codes. Documented in README.md; keep the two in step.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3  # the command finished, but some input rows could not be read


class TallyError(Exception):
    """An error with a message fit to show the user, and an exit code."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        # Lines that quote the input file. Shown only when the user asks
        # (``tally import --show-rows``); never part of str(error).
        self.details = details or []

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  {self.hint}"
        return self.message


class ConfigError(TallyError):
    """The configuration file is missing, unreadable or wrong."""


class UsageError(TallyError):
    """The command line asked for something impossible."""

    exit_code = EXIT_USAGE


class ImportError_(TallyError):
    """A statement file could not be imported at all. Nothing was changed."""


class DataLocked(TallyError):
    """Another copy of Tally holds the data file."""
