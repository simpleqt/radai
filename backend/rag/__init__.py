__all__ = ["RagService", "get_rag_service"]


def __getattr__(name):
    if name in __all__:
        from backend.rag.service import RagService, get_rag_service

        values = {
            "RagService": RagService,
            "get_rag_service": get_rag_service,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
