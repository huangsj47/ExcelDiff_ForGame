"""配表怎么读进来：工作簿 → DataFrame、行号口径、表头与列名。

## 为什么单独一个文件

`services/diff_service.py` 原先 1990 行、是一个 1917 行的类，离
`scripts/check_file_length.py --strict` 的 2000 行 ERROR 只差 10 行。这里放的是
「读」的那一半：`_read_excel_data` 把字节读成工作表，`_dataframe_rows_with_index`
给每行安上物理行号，`_build_header_rows` / `_build_column_names` 决定「第几行是表头、
列名叫什么」。它们**不参与两张表的比较**，只回答「这张表长什么样」。

## 为什么是 mixin 而不是自由函数

类里除 `self.performance_stats` 外没有实例状态，`self.X` 全是调兄弟方法。抽成 mixin
之后方法体**一行都不用改** —— `self._normalize_value(...)` 之类照旧经 MRO 解析，
`monkeypatch.setattr(DiffService, "...")` 与 `inspect.getsource(...)` 也都照旧。
类的常量与 `__init__` 仍在 `diff_service.py`，本模块的方法通过 `self.` 取用。
"""

import os
import re
from typing import Any, Dict

# ---------------------------------------------------------------------------
#  行号口径：**整个平台只有这一处实现**
# ---------------------------------------------------------------------------


def physical_row_number(index0_based: int, *, rows_before: int = 0) -> int:
    """序列里第 `index0_based` 个元素（0 起）在 Excel 文件里的**物理行号**。

    平台有两条 Excel 比较实现，**两边喂进来的序列起点不同**：

    * 主引擎 `pandas.read_excel(header=0)`：物理第 1 行被当成列名吃掉，帧里第 0 条数据
      是**物理第 2 行**（`rows_before=1`）；
    * 旧引擎 `openpyxl` 逐行读原始行（`git_excel_parser_helpers.extract_excel_data` 从
      `range(1, max_row + 1)` 起）：第 0 个元素**就是物理第 1 行**（`rows_before=0`）。

    于是同一行在两边算行号的**算式**不同（`idx + 2` 与 `i + 1`），**结果必须相同**：
    「第 N 行」是评审者回文件里核对的唯一坐标，两条路径给出不同的数就等于「同一次改动，
    页面说第 12 行、AI 说第 11 行」，而没有任何地方会报错。`rows_before` 是「这个序列前面
    已经被吃掉了几个物理行」，**不是可以随手填的数**：改了它，两条路径中的一条整体错一行。

    历史：主引擎原先写 `idx + 1`（页面行号比文件里小 1，线上 8 个分片复核 20/20 一致），
    改成 `idx + 2` 之后与旧引擎对齐 —— 旧引擎那几处一直是对的。收敛到这里之后，「不同算式」
    变成同一个函数的两个参数，再也不会有人只改一边。
    """
    return int(index0_based) + int(rows_before) + 1


class DiffExcelReaderMixin:

    def _read_excel_data(self, content: bytes, file_path: str) -> Dict[str, Any]:
        """读取Excel文件数据"""
        import io
        import warnings

        import pandas as pd

        try:
            # 根据文件扩展名选择读取方式
            ext = os.path.splitext(file_path.lower())[1]
            
            # dtype=str + keep_default_na=False：**按文本原样读取，不做类型推断、不做 NA 转换**。
            #
            # 为什么必须显式关掉这两个默认值：pandas 默认会在**读取阶段**就改写字面量，
            # 于是不等比较就已经丢掉了差异：
            #     '00123' -> int 123        前导零丢失
            #     '1.10'  -> float 1.1      尾零丢失
            #     '1e3'   -> float 1000.0   字面量被改写
            #     'TRUE'  -> bool True      大小写丢失
            #     'NULL'/'nan'/'N/A'/'None'/'NA'/'<NA>' -> NaN   **真实取值被当成空值**
            # 对本平台（变更确认）来说这是最坏的失败方式：不是报错，而是把「有变更」
            # 显示成「没有变更」，审核者看不到也就不会核对 → 直接漏审。
            #
            # 代价：diff 数量会比关闭前变多（这是预期），旧缓存由 DIFF_LOGIC_VERSION 失效。
            # 注意 keep_default_na=False 只关闭「把文本当 NA」，真正的空单元格读数仍是
            # 空值（见 _normalize_value），所以「清空单元格」这类变更不会被吃掉。
            READ_KWARGS = {'dtype': str, 'keep_default_na': False}

            if ext in ('.csv', '.tsv'):
                # 文本表格（CSV / TSV）：**按扩展名显式指定分隔符**走同一个文本解析器。
                #
                # 修前只判 `ext == '.csv'`，`.tsv` —— 它在 CSV_EXTENSIONS 里被声明支持
                # （:26），get_file_type 也判为 'excel'（:48）—— 落进了下面的 pd.ExcelFile
                # 分支，而 .tsv 不是 Excel 容器，于是整份文件以
                # "Excel file format cannot be determined" 收场，一个单元格都读不出来。
                # TSV 是配表常见导出格式，声明支持就必须真的能读。
                #
                # 分隔符只按扩展名决定（csv→','、tsv→'\t'），不做内容嗅探：
                # 嗅探会在「逗号/制表符同时出现在字段里」的表上静默选错分隔符，
                # 那正是本文件要避免的静默形态。
                separator = '\t' if ext == '.tsv' else ','
                text_content = self._decode_text(content)
                df = pd.read_csv(io.StringIO(text_content), sep=separator, **READ_KWARGS)
                return {'Sheet1': df}
            else:
                # Excel文件处理
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message='Data Validation extension is not supported and will be removed')
                    warnings.filterwarnings('ignore', message='Conditional Formatting extension is not supported and will be removed')
                    excel_file = pd.ExcelFile(io.BytesIO(content))
                    sheets = {}
                    for sheet_name in excel_file.sheet_names:
                        sheets[sheet_name] = pd.read_excel(excel_file, sheet_name=sheet_name, **READ_KWARGS)
                return self._repair_inferred_cells(content, sheets)

        except Exception as e:
            raise Exception(f"读取Excel文件失败: {str(e)}")

    def _dataframe_rows_with_index(self, df):
        """高效转换 DataFrame 为带原始行号的记录列表。

        行号是**Excel 物理行号**：`header=0` 读入时第 1 行已经当作列名吃掉，所以第 0 条
        数据的物理行号是 2 —— 算式与理由见 `physical_row_number`（口径的唯一实现）。
        修前这里是 `idx + 1`，页面上的行号比文件里真实行号小 1（线上 8 个分片独立复核
        20/20 一致，例：平台 74 ↔ Excel 75），而 `templates/help.html` 又把行号写成
        「方便快速定位」——评审者按它回文件里核对会整体错一行。
        """
        records = df.to_dict(orient='records')
        return [
            (physical_row_number(idx, rows_before=1), row_data)
            for idx, row_data in enumerate(records)
        ]

    @classmethod
    def _raw_first_row_cell(cls, name, raw_names) -> str:
        """把「改名前的列名」还原成第 1 行那一格的**原文**。

        名称行不是第 1 行时，第 1 行的内容只存在于改名前的列名里，而那份列名已经被
        pandas 改写过了，得反着还原两件事：

        * 空单元格被起了占位名（`Unnamed: 3`）—— 它的原文就是空，照原样显示出来
          只会让「改了个标题」的评审者看到一墙 `Unnamed: 3`；
        * 重名被加了 `.1`/`.2` 后缀 —— 原文没有后缀。

        两处还原都只在**能确定是合成出来的**时候做（占位名、且原名确实也在这份列名里），
        与 `_display_column_name` 的判据同源。
        """
        if cls._is_placeholder_column_name(name):
            return ''
        match = re.fullmatch(r'(.+)\.(\d+)', str(name or ''))
        if match and match.group(1) in set(raw_names):
            return match.group(1)
        return name

    @staticmethod
    def _header_row_count(raw_value) -> int:
        """把仓库配置的「表头行数」规范化成「表头块占前几行」（**含第 1 行的列名行**）。

        读取用 `header=0`，第 1 行已经被当作列名吃掉，所以：

        * `1`（或空/非法值）= 只有第 1 行是表头，**与今天逐字一致**（没有表头块）；
        * `3` = 第 1 行是列名，物理行 2、3 也是表头，它们归到 `header_rows` 里。

        「配了就一定要生效」这句话在这里的具体含义是：配了 3，第 2、3 行就不再被报成
        数据行的变更（`templates/help.html` 的「表头行数」一节承诺过这件事，
        而引擎从初始提交起就没读过这个配置）。
        """
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            return 1
        return count if count > 1 else 1

    @staticmethod
    def _header_name_row(raw_value, header_count) -> int:
        """把仓库配置的「名称行」规范化成**物理行号**（列名取第几行）。

        * 空/非法/`1` = 今天的行为：第 1 行就是字段名行，一个字都不动；
        * `2` = 第 1 行是大标题、第 2 行才是字段名（配表里很常见）；
        * 超出表头块（`> header_count`）时**收到最后一行**：名称行只能在表头块里，
          这是表单那一层也要校验的约束
          （`services/repository_diff_cache_reset.parse_header_name_row`，越界会打回
          整份表单并说明原因）；引擎这一层再夹一次是为了让手写调用（脚本、测试、
          老数据）不会越界读到数据行上去。
        """
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return 1
        if value < 2:
            return 1
        return value if value <= header_count else header_count

    def _plan_name_row(self, current_df, previous_df, name_row):
        """决定这次比较「列名换成什么」，返回 `None`（不改名）或两边的改名方案。

        为什么要有这个函数，而不是各自 `df.columns = ...` 了事：**两边必须一起改**。
        只改一边的话，`_pair_columns` 会拿「改后的名字」去比对「没改的名字」，
        每一列都被判成「旧列删除 + 新列新增」，于是**整张表每一行都会多出两处假变更**
        （这正是 `_pair_columns` 的 docstring 里那类审计事故的形态）。所以任一边取不到
        名称行（表短到根本没有那一行、或那一行没有列）时，两边都不改，退回今天的行为。

        名称行整行留白不算异常：那一行的列名会全变成占位名（`Unnamed: i`），而占位名
        本来就不当锚点、不报改名，落点与今天一致。

        返回 `{'current': (列名列表, 第 1 行字典), 'previous': (…)}`。第 1 行的字典是
        `_build_header_rows` 要用的：改名之后，第 1 行的原文与列身份再无关系，
        只能靠这里顺带留下的一份（`header=0` 把它读成了列名）。
        """
        if name_row <= 1 or current_df is None or previous_df is None:
            return None
        plan = {}
        for side, df in (('current', current_df), ('previous', previous_df)):
            index = name_row - 2        # `header=0` 下物理行 p ↔ 帧索引 p-2
            if index >= len(df) or df.shape[1] == 0:
                return None
            raw_names = [str(name) for name in df.columns]
            names = self._build_column_names(list(df.iloc[index]))
            plan[side] = (names, {names[i]: self._raw_first_row_cell(raw_names[i], raw_names)
                                  for i in range(len(names))})
        return plan

    def _rename_by_name_row(self, df, name_row):
        """单边改名（整张工作表新增/删除时只有一边有帧）。

        返回 `(帧, 第 1 行字典)`；取不到名称行时原样返回 `(帧, None)` —— 与
        `_plan_name_row` 同一口径。返回的是 `set_axis` 出来的**新帧**（列名换了、
        数据共用），调用方拿到的原帧一个字节都不变。
        """
        plan = self._plan_name_row(df, df, name_row)
        if plan is None:
            return df, None
        return df.set_axis(plan['current'][0], axis=1), plan['current'][1]

    def _build_column_names(self, cells) -> list:
        """一行的单元格文本 → 列名列表，**规则与 pandas 的 `header=0` 一致**。

        为什么必须自己算这一遍：`_pair_columns`（同名锚点 + 等宽顺序配对）、
        `_is_placeholder_column_name`（`Unnamed: 7` 不当锚点、不报改名）、
        `_display_column_name`（`X.1` 渲染成「同名列第 2 个」）三处都建立在这套命名
        约定上。名称行读出来的这一行如果不按同样的规则命名，三处一起失效 ——
        重名列会被当成不同的列、空列名会被当成有名字的列，而且失效方式是静默的
        （多报/漏报列变更，不报错）。

        算法是 pandas 那份的逐句移植（`pandas/io/parsers/python_parser.py` 的
        `col_loop_order` 那一段），两处细节都不能省：

        * 空单元格（`None`/NaN/空串，口径同 `_normalize_value`）→ `Unnamed: i`，
          `i` 是**位置**（与 pandas 的 `Unnamed: 0` 起算一致）。空白串（`'  '`）是
          **取值**，不是空 —— 与 `_values_equal` 同一口径。
        * 重名从 `.1` 起往后找**第一个没被占用的**后缀（`['id','id','id.1']` →
          `['id','id.2','id.1']`，不是 `id.1` 撞 `id.1`）；且**具名列先命名、占位名
          后命名**，否则「表里真有一列叫 `Unnamed: 2`」的表上，谁被改名会与今天相反。

        读取口径的差别只剩一处：名称行是**按 dtype=str 读成文本**的，所以数字表头
        出来是 `'1'` 而不是 pandas 表头解析得到的 `1`（`'00123'` 也因此保住了前导零）。
        这是本平台「按文本原样读」的读取纪律，不是偏差。
        """
        names = []
        unnamed = []
        for index, cell in enumerate(cells):
            value = self._normalize_value(cell)
            if value is None:
                names.append(f'Unnamed: {index}')
                unnamed.append(index)
            else:
                names.append(str(value))
        counts: Dict[Any, int] = {}
        # 具名列先、占位名后（pandas 的 `col_loop_order`）。
        unnamed_set = set(unnamed)
        order = [i for i in range(len(names)) if i not in unnamed_set] + unnamed
        for index in order:
            base = names[index]
            name = base
            count = counts.get(name, 0)
            while count > 0:
                counts[base] = count + 1
                name = f'{base}.{count}'
                if name in names:
                    count += 1
                else:
                    count = counts.get(name, 0)
            names[index] = name
            counts[name] = count + 1
        return names

    def _build_header_rows(self, current_pairs, previous_pairs, columns, header_count,
                           name_row: int = 1, first_rows=None):
        """把物理行 `2..header_count`（表头块）单独算一份 diff，返回 `(rows, stats)`。

        **为什么不把表头行留在 `rows` 里**：`rows` 的契约是「有变更的数据行」，
        表头行整块常驻，四处下游会按这个契约把它们弄错 ——
        `optimize_diff_data` 的状态白名单（未知状态写缓存时被静默删掉）、
        `validate_excel_diff_data` 的「有没有变更」（常驻行会让「完全没变」也判成有内容）、
        前端 `groupChangedRows` 的分组顺序（未知状态落到表尾）、
        AI 摘要的变更行过滤（`status not in ("", "unchanged")` 就算变更）。
        放进独立键这四处都不受影响（`optimize_diff_data` 用 `dict(sheet_data)` 复制整张表）。

        配对按**行号**（第 2 行对第 2 行）：表头是固定位置的几行，不是可增删的数据行，
        相似度匹配在这里只会把「表头第 2 行」配到别处去。

        未改动的行**也留在结果里**（界面据此写「表头 3 行 · 无改动」并让评审者看到
        表头长什么样），所以判「有没有变」不能看列表是否为空 ——
        用 `utils.diff_data_utils.header_rows_have_changes`。

        `name_row > 1`（列名取自第 2 行及以后）时，块里要**排除名称行**（它的改动由
        `header_changes` 的列改名/增删表达，同一件事不报两遍），并**补上第 1 行**
        （`first_rows`，见 `_plan_name_row`）—— 否则「只改标题行」会什么也不显示，
        比修之前更差：那时它至少会以「某一列改了名」的形式冒出来。
        """
        def _block(pairs, first_row):
            block = {
                row_number: row_data
                for row_number, row_data in pairs
                if row_number <= header_count and row_number != name_row
            }
            # 第 1 行整行没有内容（例如它只是一行留白）时不补 —— 与数据行同一判空口径，
            # 免得表头块里凭空多出一行空格子。两边都留白时它同样不会出现。
            if first_row is not None and self._has_valid_data(first_row, columns):
                block[1] = first_row
            return block

        current_first, previous_first = first_rows if first_rows else (None, None)
        current_block = _block(current_pairs, current_first)
        previous_block = _block(previous_pairs, previous_first)

        rows = []
        for row_number in sorted(set(current_block) | set(previous_block)):
            current_row = current_block.get(row_number)
            previous_row = previous_block.get(row_number)
            if current_row is None:
                # 表头行本身被删了（表头行数配得比实际多，或提交里抽掉一行表头）。
                rows.append({
                    'row_number': row_number,
                    'status': 'removed',
                    'data': previous_row,
                })
            elif previous_row is None:
                rows.append({
                    'row_number': row_number,
                    'status': 'added',
                    'data': current_row,
                })
            elif self._rows_equal(current_row, previous_row, columns):
                rows.append({
                    'row_number': row_number,
                    'status': 'unchanged',
                    'data': current_row,
                })
            else:
                rows.append({
                    'row_number': row_number,
                    'status': 'modified',
                    'data': current_row,
                    'cell_changes': self._row_cell_changes(current_row, previous_row, columns),
                })

        stats = {
            'total_rows_current': len(current_block),
            'total_rows_previous': len(previous_block),
            'added': len([r for r in rows if r['status'] == 'added']),
            'removed': len([r for r in rows if r['status'] == 'removed']),
            'modified': len([r for r in rows if r['status'] == 'modified']),
        }
        return rows, stats

    @staticmethod
    def _with_header_block(result, header_block, header_stats):
        """把表头块挂到工作表结果上。

        表头块为空（没配表头行数、或那几行整行空白）就**不加这两个键** ——
        未配置的仓库载荷与今天逐字一致，缓存与断言都不受影响。
        """
        if header_block:
            result['header_rows'] = header_block
            result['header_stats'] = header_stats
        return result
