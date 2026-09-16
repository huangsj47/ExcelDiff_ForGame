"""重建数据库（drop_all + create_all）。

**破坏性操作**：只应在临时库/测试库上跑。守卫见下方 assert_destructive_db_allowed
—— 不设 ALLOW_DESTRUCTIVE_DB_OPS 时它会直接拒绝，不会真的 drop。
"""

import sys
from pathlib import Path

# `python scripts/recreate_db.py` 时 sys.path[0] 是 scripts/，不是仓库根，
# 下面两行 import 会 ModuleNotFoundError。原先躺在根目录才恰好能跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.model_loader import get_runtime_models  # noqa: E402
from utils.db_safety import assert_destructive_db_allowed  # noqa: E402

if __name__ == '__main__':
    app, db = get_runtime_models("app", "db")
    with app.app_context():
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="scripts/recreate_db.py::drop_all",
            testing=bool(app.config.get("TESTING")),
        )
        db.drop_all()
        db.create_all()
        print('Database recreated with clone status fields')
