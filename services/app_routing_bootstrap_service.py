"""App routing/bootstrap helpers extracted from app.py."""

from __future__ import annotations

from werkzeug.routing import IntegerConverter, Rule, ValidationError

from utils.diff_data_utils import format_cell_value, get_excel_column_letter

# SQLite 的 INTEGER 是 64 位有符号数。**Werkzeug 的 `<int:…>` 转换器没有上界**：
# `/\d+/` + 裸 `int()`，所以 `/ai-analysis/runs/99999999999999999999/usage` 会正常匹配、
# handler 拿到一个 10^20 的整数，第一句 `db.session.get(Model, id)` 就在 pysqlite 里抛
# `OverflowError: Python int too large to convert to SQLite INTEGER` —— 那不是
# `SQLAlchemyError`，仓库里也没有兜它的错误处理器，于是任何登录用户都能拿一条 URL 打出 500。
#
# 在这条路上拦掉：超出这个范围的 id **不可能存在**，让它当作路由不匹配（→ 404），
# 比在几十个 handler 里各写一遍边界判断可靠得多。
MAX_INTEGER_ID = 2**63 - 1


class BoundedIntegerConverter(IntegerConverter):
    """给内置的 `<int:…>` 加上界。超出上界的值按「不匹配」处理（最终是 404）。"""

    def to_python(self, value: str) -> int:
        number = super().to_python(value)
        if number > MAX_INTEGER_ID:
            # `ValidationError` 在 werkzeug 的路由匹配里表示「这条规则不匹配」，
            # 于是请求落到 404，而不是带着一个查不出来的 id 进 handler。
            raise ValidationError()
        return number


def bound_integer_url_converter(app) -> None:
    """覆盖内置的 `int` 转换器（对所有 `<int:…>` 路由生效）。"""
    app.url_map.converters["int"] = BoundedIntegerConverter


DEFAULT_BLUEPRINT_PREFIXES = (
    "core_management_routes.",
    "commit_diff_routes.",
    "weekly_version_routes.",
    "cache_management.",
    "agent_management_routes.",
    "ai_analysis_routes.",
    "main.",
)


def _register_endpoint_aliases(app, log_print, bp_prefixes):
    """Register short endpoint aliases for blueprint-prefixed endpoints."""
    alias_rules = []
    for rule in app.url_map.iter_rules():
        for prefix in bp_prefixes:
            if rule.endpoint.startswith(prefix):
                short_name = rule.endpoint[len(prefix):]
                if short_name in app.view_functions:
                    break
                app.view_functions[short_name] = app.view_functions[rule.endpoint]
                new_rule = Rule(
                    rule.rule,
                    endpoint=short_name,
                    methods=rule.methods,
                    defaults=rule.defaults,
                    subdomain=rule.subdomain,
                    strict_slashes=rule.strict_slashes,
                    merge_slashes=rule.merge_slashes,
                    redirect_to=rule.redirect_to,
                )
                alias_rules.append(new_rule)
                break

    for new_rule in alias_rules:
        app.url_map.add(new_rule)
    log_print(f"[TRACE] Registered {len(alias_rules)} endpoint short-name aliases", "APP")


def _register_template_filters(app):
    """Register template filters used by diff templates."""

    def _excel_column_letter_filter(index):
        return get_excel_column_letter(index)

    def _format_cell_value_filter(value):
        return format_cell_value(value)

    app.add_template_filter(_excel_column_letter_filter, "excel_column_letter")
    app.add_template_filter(_format_cell_value_filter, "format_cell_value")


def configure_app_routing_bootstrap(*, app, log_print, bp_prefixes=None):
    """Configure endpoint aliases and template filters on startup."""
    prefixes = tuple(bp_prefixes) if bp_prefixes else DEFAULT_BLUEPRINT_PREFIXES
    _register_endpoint_aliases(app, log_print, prefixes)
    _register_template_filters(app)
