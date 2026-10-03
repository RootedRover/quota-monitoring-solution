"""Validation for BigQuery identifiers.

BigQuery will not let you bind a table or view name as a query parameter, so
any code that builds a fully-qualified reference is necessarily doing string
interpolation. That is fine as long as the pieces are known to be identifiers
and nothing else, which is what this module establishes.

Both the collector and the dashboard take their project and dataset from the
environment, and an environment variable is a perfectly ordinary way for
something hostile to arrive. Validating once, at the boundary, is cheaper than
reasoning about each call site.
"""

from __future__ import annotations

import re

# Deliberately stricter than BigQuery itself. Dataset and table names allow
# only letters, digits and underscores; project ids also allow hyphens. Nothing
# here can contain a backtick, a quote, a semicolon or whitespace, so a
# validated value cannot break out of the backticks it is placed in.
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_NAME = re.compile(r"[A-Za-z0-9_]{1,1024}")


def validate_project(project: str) -> str:
    if not _PROJECT.fullmatch(project):
        raise ValueError(f"not a valid GCP project id: {project!r}")
    return project


def validate_name(name: str) -> str:
    """Validate a dataset, table or view name."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"not a valid BigQuery identifier: {name!r}")
    return name


def qualified(project: str, dataset: str, name: str) -> str:
    """``project.dataset.name``, with every part validated."""
    return f"{validate_project(project)}.{validate_name(dataset)}.{validate_name(name)}"
