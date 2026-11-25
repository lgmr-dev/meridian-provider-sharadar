class PluginError(Exception):
    status_code = 503

    def __init__(self, code: str, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        if status_code is not None:
            self.status_code = status_code


class PluginUnavailable(PluginError):
    status_code = 503


class PluginCrashed(PluginError):
    status_code = 502

    def __init__(self, message: str = "") -> None:
        super().__init__("plugin_crashed", message)


class PluginTimeout(PluginError):
    status_code = 504

    def __init__(self, message: str = "") -> None:
        super().__init__("plugin_timeout", message)


class PluginProtocolError(PluginError):
    status_code = 502

    def __init__(self, code: str = "plugin_protocol_error", message: str = "") -> None:
        super().__init__(code, message)


class UnsupportedOperation(PluginError):
    status_code = 501

    def __init__(self, operation: str) -> None:
        super().__init__("unsupported_operation", f"operation {operation!r} is not supported")
