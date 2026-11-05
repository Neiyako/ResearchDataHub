"""配置验证子包：组合检查器 → :class:`~researchdatahub.environment.models.EnvironmentReport`。

公开入口
--------
* :class:`~researchdatahub.environment.validation.validator.ConfigurationValidator`
* :func:`~researchdatahub.environment.validation.validator.validate_environment`
* :func:`~researchdatahub.environment.validation.validator.derive_overall_status`
* :func:`~researchdatahub.environment.validation.validator.render_guide`
"""

from researchdatahub.environment.validation.validator import (
    DEFAULT_STORAGE_ROOT,
    STRUCTURAL_FAILURE_STATUSES,
    ConfigurationValidator,
    derive_overall_status,
    render_guide,
    validate_environment,
)

__all__ = [
    "DEFAULT_STORAGE_ROOT",
    "STRUCTURAL_FAILURE_STATUSES",
    "ConfigurationValidator",
    "derive_overall_status",
    "render_guide",
    "validate_environment",
]
