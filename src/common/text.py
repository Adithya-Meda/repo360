"""Text formatting and dynamic grammatical pluralization helpers for Repo360."""

from __future__ import annotations

from typing import Optional, Union


def pluralize(
    count: Union[int, float],
    singular: str,
    plural: Optional[str] = None,
    include_count: bool = False,
) -> str:
    """Return grammatically correct singular or plural form based on count.

    Args:
        count: The numeric count determining grammatical number.
        singular: The singular noun form (e.g. 'repository', 'organization', 'runner').
        plural: Optional explicit plural form (e.g. 'repositories'). If omitted, standard
            English rules are automatically applied (-y -> -ies, -s/-x/-ch/-sh -> -es, etc.).
        include_count: Whether to prepend the formatted count string (e.g. '1 repository', '5 repositories').

    Examples:
        >>> pluralize(1, "repository")
        'repository'
        >>> pluralize(2, "repository")
        'repositories'
        >>> pluralize(1, "organization", include_count=True)
        '1 organization'
        >>> pluralize(3, "organization", include_count=True)
        '3 organizations'
    """
    if plural is None:
        if singular.endswith("y") and not singular.endswith(("ay", "ey", "iy", "oy", "uy")):
            plural = singular[:-1] + "ies"
        elif singular.endswith(("s", "sh", "ch", "x", "z")):
            plural = singular + "es"
        else:
            plural = singular + "s"

    word = singular if count == 1 else plural
    if include_count:
        if isinstance(count, int):
            return f"{count:,} {word}"
        return f"{count} {word}"
    return word
