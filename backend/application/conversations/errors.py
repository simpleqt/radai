"""Conversation state conflicts shared by persistence backends."""


class ConversationStateError(RuntimeError):
    """Raised when a thread cannot be safely reused by an agent request."""


class ConversationAgentMismatchError(ConversationStateError):
    """The external thread already belongs to another agent profile."""


class ConversationDeletedError(ConversationStateError):
    """A deleted thread id must not be revived with stale checkpoint state."""


class ConversationBusyError(ConversationStateError):
    """Another request currently owns the conversation row lock."""
