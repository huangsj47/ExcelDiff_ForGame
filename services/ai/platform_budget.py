"""平台总预算的读取与保存（单行表 `ai_platform_budget`）。

## 校验**复用项目配置那一套**

三个字段（周期 / token 上限 / 费用上限）与项目档是**同名同规则**的，所以这里不新写一份
校验，直接调 `endpoint_service.validate_field` —— 它的规则表 `FIELD_RULES` 是界面文案、
服务端校验、默认值三者的唯一事实源。另写一份的后果：平台上允许填 0（= 锁死全平台）、
项目上不允许，而这种不一致在界面上根本看不出来。

## 保存是「部分更新」

只处理提交里出现的键，与 `validate_payload` 同一口径（界面可能只改一栏）。没提交的键
**保持原值**，不会因为一次只改周期的保存把上限清空。
"""
from __future__ import annotations

from typing import Any, Mapping

from models import db
from models.ai_analysis import PLATFORM_BUDGET_DEFAULTS, SINGLETON_ID, AiPlatformBudget
from services.ai.endpoint_service import (
    ConfigValidationError,
    FieldError,
    validate_field,
)

# 平台档能改的字段。与项目档同名 —— 这是有意的：界面、文档、日志里说的「预算周期」
# 在两处必须是同一个词，否则用户会以为它们不是一回事。
BUDGET_FIELDS = ("budget_period", "budget_token_limit", "budget_cost_limit")

# 字段 → 结果里的键。存的是 `budget_*`（与项目档同名列），读出来给界面时去掉前缀，
# 让「平台预算」这张卡片不必知道自己内部叫什么。
_FIELD_TO_KEY = {
    "budget_period": "period",
    "budget_token_limit": "token_limit",
    "budget_cost_limit": "cost_limit",
}

# 字段 → **数据库列名**。写库时用的是这一张，不是上面那张。
#
# 这两张表必须分开：`_FIELD_TO_KEY` 是**展示**用的键（界面读 `token_limit`），而列名是
# 存储用的。第一版图省事拿 `_FIELD_TO_KEY` 去 `setattr`，于是 `setattr(row,
# "budget_token_limit", 300)` 在 SQLAlchemy 模型上**成功地**创了一个游离的实例属性 ——
# 不报错、不写库，`get_platform_budget()` 读回来还是默认值。表现是「保存成功、刷新就没了」。
# 这一处的教训值得留在代码里：静默什么都没写，比抛异常难查得多。
_FIELD_TO_COLUMN = {
    "budget_period": "period",
    "budget_token_limit": "token_limit",
    "budget_cost_limit": "cost_limit",
}


def _column_for(field: str) -> str:
    """取字段对应的列名，并在**映射写错时立刻炸**。

    宁可在这里抛一个明确的异常，也不要 `setattr` 出一个游离属性把「保存」变成
    「什么都没发生」—— 那种失败在界面上是看不出来的。
    """
    column = _FIELD_TO_COLUMN.get(field)
    if column is None:
        raise KeyError(f"{field} 没有对应的数据库列（见 _FIELD_TO_COLUMN）")
    if column not in AiPlatformBudget.__table__.columns:
        raise KeyError(
            f"{field} 映射到的列「{column}」在 {AiPlatformBudget.__tablename__} 里不存在"
        )
    return column


def _row() -> AiPlatformBudget | None:
    return db.session.get(AiPlatformBudget, SINGLETON_ID)


def get_platform_budget() -> dict[str, Any]:
    """读平台总预算。**没有这一行时返回默认值**，而不是 `None`。

    返回 `None` 会迫使每一个调用点各写一次兜底。这里一律给出完整字典，`configured`
    说明平台管理员有没有显式保存过上限。
    """
    row = _row()
    if row is None:
        return {
            "period": PLATFORM_BUDGET_DEFAULTS["period"],
            "token_limit": PLATFORM_BUDGET_DEFAULTS["token_limit"],
            "cost_limit": PLATFORM_BUDGET_DEFAULTS["cost_limit"],
            "configured": False,
            "updated_by": "",
            "updated_at": None,
        }
    return row.resolved()


def set_platform_budget(
    payload: Mapping[str, Any] | None, *, updated_by: str = ""
) -> tuple[bool, str, list[dict]]:
    """保存平台总预算。返回 `(成功, 一句话, 字段级错误)`。

    与项目配置的保存同一形状（`update_project_analysis_config`），这样路由层不必为两处
    写两种错误处理。**校验失败时一个字段都不写**：部分写入会得到一个「周期改了、上限
    没改」的中间状态，而用户看到的是「保存失败」——他不会想到失败前已经改了一半。
    """
    data = dict(payload or {})
    errors: list[FieldError] = []
    normalized: dict[str, Any] = {}
    for field in BUDGET_FIELDS:
        if field not in data:
            continue
        try:
            normalized[field] = validate_field(field, data[field])
        except FieldError as exc:
            errors.append(exc)
        except ConfigValidationError as exc:  # 该字段的校验器抛了整套错误
            errors.extend(exc.errors)

    if errors:
        return (
            False,
            "；".join(f"{item.label}：{item.message}" for item in errors) or "保存失败。",
            [item.as_dict() for item in errors],
        )

    if not normalized:
        return True, "没有需要修改的字段。", []

    row = _row()
    try:
        if row is None:
            row = AiPlatformBudget(id=SINGLETON_ID)
            db.session.add(row)
        for field, value in normalized.items():
            setattr(row, _column_for(field), value)
        row.updated_by = (updated_by or "")[:100]
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 落库失败要回一句人话，不是 500 堆栈
        db.session.rollback()
        return False, f"保存失败：{exc}", []

    changed = "、".join(
        _FIELD_TO_KEY.get(field, field) for field in sorted(normalized)
    )
    return True, f"已保存平台总预算（{changed}）。", []


def platform_budget_public() -> dict[str, Any]:
    """给界面的一份：配置 + 当前是否限制。

    与 `get_platform_budget` 的差别只有一个 —— 这里把「配了两列都为 NULL」与「没配过」
    都归到 `configured=False`（`resolved()` 已经这么做了），而 `period` 仍然给出实际
    生效值，因为「周期是哪个」在两种情况下都有意义（界面要显示它、联动要按它算）。
    """
    data = get_platform_budget()
    return {
        "period": data.get("period") or PLATFORM_BUDGET_DEFAULTS["period"],
        "token_limit": data.get("token_limit"),
        "cost_limit": data.get("cost_limit"),
        "configured": bool(data.get("configured")),
        "updated_by": data.get("updated_by") or "",
        "updated_at": data.get("updated_at"),
    }
