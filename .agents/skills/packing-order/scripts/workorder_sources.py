"""Shared routing for packaging workorder creation methods."""

MANUAL_SOURCE_PREFIXES = ('19', '1018', '1008', '1012')


def is_manual_source(document_no):
    """Classify the real source document number without rewriting its value."""
    return isinstance(document_no, str) and document_no.startswith(MANUAL_SOURCE_PREFIXES)
