"""应用错误类型。"""


class TUICodingAgentError(Exception):
    """所有可预期应用错误的基类。"""


class ConfigError(TUICodingAgentError):
    """配置无法安全加载。"""


class ProviderError(TUICodingAgentError):
    """模型服务调用失败。"""


class AuthenticationError(ProviderError):
    """API 认证或授权失败。"""


class RateLimitError(ProviderError):
    """请求受到服务端限流。"""


class NetworkError(ProviderError):
    """连接、超时或网络传输失败。"""


class ProtocolError(ProviderError):
    """服务端返回了无法解析的协议数据。"""
