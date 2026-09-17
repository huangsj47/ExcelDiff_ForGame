# -*- coding: utf-8 -*-
"""仓库 / 周版本配置的**唯一**排序口径。

## 为什么需要它

「第一个仓库」在三个地方各有一个答案，而且互不相同：

* 合并视图的进入链接用 `version.configs[0].id`，而那个列表来自
  `order_by(WeeklyVersionConfig.created_at.desc())` —— 是**最后新建**的仓库；
* diff 页的标签顺序是 `repository.name` 升序；
* 配置列表 API 没有任何 `order_by`，顺序由数据库决定。

于是「点『查看版本diff』进去，高亮的不是第一个标签」是必然结果，不是偶发。

## 排序键为什么是这个形状

`(显示顺序, 名称, id)`，逐级兜底：

1. **`display_order`** —— 平台既有的「用户排过的仓库顺序」（拖拽排序
   `services/repository_admin_handlers.py` 写、编辑表单也写）。但它**单独用不了**：
   所有仓库默认都是 0，编辑表单能写入任意整数，拖拽接口只交换两个值不做重编号，
   所以重复值很常见。
2. **`name`** —— 显示顺序并列时按名称（码点序，与语言环境无关，因此跨机器确定）。
   对**没人排过顺序**的项目（全是 0），这一级就退化成「按名称排」，
   与 diff 页原来的标签顺序一致 —— 存量部署不会看到顺序变化。
3. **`id`** —— 最后兜底，使键成为全序：没有任何两个 config 会打平。

`display_order` 读取时**必须容错**：它是从表单 `int()` 出来的，历史数据里可能是
NULL 或脏值。一个脏值把整页打成 500 是不可接受的，所以取不到就当 0。
"""


def repository_order_key(repository):
    """单个仓库的排序键。`repository` 可以为 None（仓库被删但配置还在）。

    首元素是「仓库是否缺失」的标记位，缺失的恒排最后。做成标记位而不是给个
    大数/`inf`，是为了让「缺失」与「某个真实 display_order」永远不可能打平 ——
    否则一个 display_order=1 的真实仓库会和缺失项在第一级并列。
    """
    if repository is None:
        return (1, 0, "", 0)
    try:
        order = int(getattr(repository, "display_order", 0) or 0)
    except (TypeError, ValueError):
        # 脏值不能掀翻整页。它只影响排序，不该让页面 500。
        order = 0
    return (
        0,
        order,
        str(getattr(repository, "name", "") or ""),
        int(getattr(repository, "id", 0) or 0),
    )


def weekly_config_order_key(config):
    """周版本配置的排序键（= 它所属仓库的顺序，再按 config id 兜底）。

    仓库缺失的配置排在最后（`repository_order_key` 的首位标记），而不是抛异常：
    外键被删但配置行还在时，页面仍然要能打开。
    """
    return (
        *repository_order_key(getattr(config, "repository", None)),
        int(getattr(config, "id", 0) or 0),
    )
