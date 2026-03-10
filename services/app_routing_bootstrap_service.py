"""App routing/bootstrap helpers extracted from app.py."""

from __future__ import annotations

from werkzeug.routing import Rule

from utils.diff_data_utils import format_cell_value, get_excel_column_letter


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
