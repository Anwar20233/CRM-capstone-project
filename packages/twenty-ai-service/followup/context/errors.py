class ContextLoadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class LlmExtractError(Exception):
    pass
