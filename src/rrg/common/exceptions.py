class RRGError(Exception):
    """Base class for all Rotation Compass errors."""


class ConfigError(RRGError):
    pass


class ValidationCritical(RRGError):
    pass


class DataSourceError(RRGError):
    pass
