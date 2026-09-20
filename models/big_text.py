"""长文本列的统一类型：SQLite 上仍是 `TEXT`，MySQL 上给 `LONGTEXT`。

**为什么需要它**

`db.Text` 在 SQLite 里没有长度上限，在 MySQL 里编译成 `TEXT`，上限 **65,535 字节**
（utf8mb4 下大约 16,000 个汉字）。本项目的实际写入量远超这个数：

    ai_analysis_run.request_payload   496,496 字节
    diff_cache.diff_data           7,103,278 字节

也就是说，同一份代码在 SQLite 上跑得好好的，换到 MySQL 上会在写库那一刻报
`Data too long for column`——而这时分析已经跑完了，几十分钟的模型调用全白费。
SQLite 不会替我们暴露这个差异，所以只能在类型上一次性说清楚。

没有走「给这些列都加 `length=`」的路子：MySQL 的 `TEXT` 家族里只有 `LONGTEXT`
（4 GiB）够用，而 `LONGTEXT` 和 `MEDIUMTEXT` 都不是「指定长度」能选出来的，
必须换类型。

**为什么不锁死成 LONGTEXT**

本地与 CI 都跑 SQLite，`with_variant` 让两边各自拿到合适的类型：SQLite 分支保持
`TEXT`（无上限，行为不变），只有 MySQL 分支升级。这样本地库不需要重建。

**新增列怎么办**

`tests/test_mysql_text_columns.py` 会按 MySQL 方言编译全部模型建表语句，出现任何
`TEXT`（而非 `LONGTEXT`）都会失败——所以新列写 `db.Text` 会当场被拦下。
"""

from sqlalchemy.dialects.mysql import LONGTEXT

from . import db

# 共享同一个类型实例：SQLAlchemy 的类型对象设计上就是无状态的，可以跨列复用。
BigText = db.Text().with_variant(LONGTEXT(), "mysql")
