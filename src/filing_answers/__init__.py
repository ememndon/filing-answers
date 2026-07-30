"""Answers questions about company filings, and refuses to answer badly.

The package is arranged around the path a question takes:

    edgar    fetching a filing from the SEC
    extract  turning a filing into passages worth searching
    answer   question + passages -> an answer and the passage it came from
    verify   the check that rejects an answer its own source does not support
    api      the service that exposes all of it

`verify` is the reason the rest exists. Everything else is plumbing that
could be swapped out; that module is the promise the project makes.
"""

__version__ = "0.1.0"
