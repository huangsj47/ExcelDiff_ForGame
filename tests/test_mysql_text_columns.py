# -*- coding: utf-8 -*-
"""MySQL 上限：模型里不该再出现会在 MySQL 上编译成 `TEXT` 的长文本列。

## 这条为什么值得单独守

`db.Text` 在 SQLite 上没有长度上限，在 MySQL 上编译成 `TEXT`，上限 **65,535 字节**
（utf8mb4 下约 16,000 个汉字）。而本项目的实际写入量（本地实例实测）：

    ai_analysis_run.request_payload   496,496 字节
    diff_cache.diff_data           7,103,278 字节

**本地和 CI 全跑 SQLite，所以这个差异在本机永远不会暴露**：测试全绿、手工点一遍也
正常，一直到某天把库换成 MySQL，才会在写库那一刻报 `Data too long for column`——
而那时分析已经跑完，几十分钟的模型调用全白费，重跑一次还是一样的结果。

这不是「可能发生」，是**必然发生**：上面两个数就是本地实例里真实行的大小。

## 守的是什么

按 **MySQL 方言**编译每一列的类型，凡 `sqlalchemy.Text` 却拿不到 `LONGTEXT` 的，
一律失败并把 `表.列` 名字报出来。判据是**编译结果**而不是源码里写的是什么——
所以直接写 `from sqlalchemy import Text` 也逃不掉。

**为什么不能写成「源码里搜 `db.Text`」**：仓库注释里会原样引用要禁掉的写法
（本文件这段就是），按文本搜会既假红又假绿。这里按类型对象判，注释怎么写都不影响。
"""

import os
import re
import sys

import pytest
from sqlalchemy import Text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models import db  # noqa: E402

# 本地实例里实测超过 65,535 字节的两列。它们必须在这个检查的覆盖范围内，
# 否则说明遍历根本没扫到要紧的表（见 test_guard_actually_covers_the_tables_it_claims）。
_MEASURED_OVERSIZED = [
    ("ai_analysis_run", "request_payload"),
    ("diff_cache", "diff_data"),
]


def _mysql_type_of(column) -> str:
    """把一列的类型按 MySQL 方言编译出来，例如 `LONGTEXT` / `VARCHAR(255)`。"""
    return str(column.type.compile(dialect=mysql.dialect())).upper()


def _text_columns():
    """所有在 Python 侧声明为文本类型的列（含 `db.Text`、`sqlalchemy.Text`）。"""
    found = []
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Text):
                found.append((table.name, column.name, column))
    return found


def test_no_model_column_compiles_to_mysql_text():
    """任何文本列在 MySQL 上都必须是 LONGTEXT，不能是 TEXT。"""
    offenders = []
    for table_name, column_name, column in _text_columns():
        compiled = _mysql_type_of(column)
        if compiled != "LONGTEXT":
            offenders.append(f"{table_name}.{column_name} -> {compiled}")

    assert not offenders, (
        "下列列在 MySQL 上编译成了 TEXT（上限 65,535 字节），写超长内容会报 "
        "「Data too long for column」。请把类型换成 `BigText`"
        "（`from models.big_text import BigText`）：\n  " + "\n  ".join(sorted(offenders))
    )


def test_guard_actually_covers_the_tables_it_claims():
    """防「假通过」：遍历要是没扫到东西，上面那条测试会毫无理由地全绿。"""
    columns = _text_columns()
    assert len(columns) >= 50, (
        f"只扫到 {len(columns)} 个文本列，远少于预期——遍历逻辑可能已经失效，"
        "上面那条测试此刻是假通过。"
    )

    scanned = {(t, c) for t, c, _ in columns}
    for pair in _MEASURED_OVERSIZED:
        assert pair in scanned, (
            f"{pair[0]}.{pair[1]} 没有出现在被检查的文本列里。这一列本地实测已达 "
            f"{'496,496' if pair[0] == 'ai_analysis_run' else '7,103,278'} 字节，"
            "是这条护栏最该盯住的地方。"
        )


def test_compiled_create_table_has_no_bare_text():
    """整表 DDL 级别再兜一次：编译出来的建表语句里不该出现裸 `TEXT`。

    上一条件是按列判的，这条按**最终发给 MySQL 的语句**判。`LONGTEXT` / `MEDIUMTEXT`
    里的 `TEXT` 前后都是单词字符，`\\bTEXT\\b` 不会误命中。
    """
    dialect = mysql.dialect()
    compiled_tables = 0
    for table in db.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        compiled_tables += 1
        match = re.search(r"\bTEXT\b", ddl)
        assert match is None, (
            f"表 {table.name} 的 MySQL 建表语句里出现了裸 TEXT：\n"
            + "\n".join(
                line.strip() for line in ddl.splitlines() if re.search(r"\bTEXT\b", line)
            )
        )

    assert compiled_tables >= 40, f"只编译了 {compiled_tables} 张表，遍历逻辑可疑。"


def test_big_text_keeps_sqlite_on_plain_text():
    """SQLite 分支要保持 `TEXT`：本地库已建成这样，改成别的类型会要求重建库。"""
    from models.big_text import BigText

    assert str(BigText.compile(dialect=mysql.dialect())).upper() == "LONGTEXT"
    assert BigText.compile().upper() == "TEXT"


@pytest.mark.parametrize("table_name,column_name", _MEASURED_OVERSIZED)
def test_measured_oversized_columns_are_big_text(table_name, column_name):
    """把本地实测的超限行所对应的两列单独钉一遍，附上实测数字当解释。"""
    from models.big_text import BigText

    column = db.metadata.tables[table_name].columns[column_name]
    assert isinstance(column.type, type(BigText)) or isinstance(column.type, Text)
    assert _mysql_type_of(column) == "LONGTEXT"
    assert column.type is BigText, (
        f"{table_name}.{column_name} 应当直接用 `BigText`，而不是另写一个等价类型——"
        "统一到一个类型上，将来要改（比如换 MEDIUMTEXT 或加长度约束）只有一处。"
    )
