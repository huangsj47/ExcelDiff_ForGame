"""Template context bootstrap helpers extracted from app.py."""

from __future__ import annotations


def configure_template_context_processors(
    *,
    app,
    get_diff_data,
    generate_commit_diff_url,
    generate_excel_diff_data_url,
    generate_refresh_diff_url,
) -> None:
    """Register shared template helpers used by diff pages."""

    @app.context_processor
    def _inject_template_functions():
        return dict(
            get_diff_data=get_diff_data,
            generate_commit_diff_url=generate_commit_diff_url,
            generate_excel_diff_data_url=generate_excel_diff_data_url,
            generate_refresh_diff_url=generate_refresh_diff_url,
        )
