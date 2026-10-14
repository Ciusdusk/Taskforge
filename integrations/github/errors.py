"""Non-sensitive business failures; messages never include remote payloads."""


class GitHubError(Exception):
    """A bounded-attempt business failure."""


class AuthenticationError(GitHubError):
    pass


class ConfigurationError(GitHubError):
    pass


class StateError(GitHubError):
    pass


class GenerationError(StateError):
    pass


class CommitUncertainError(StateError):
    """An independent completion check also failed; do not apply again."""


class LockError(GitHubError):
    pass


class HTTPError(GitHubError):
    pass


class BudgetError(HTTPError):
    pass


class PaginationError(HTTPError):
    pass
