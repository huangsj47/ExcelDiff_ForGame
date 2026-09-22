"""两张表怎么比：DataFrame 级比较引擎与列配对。

## 为什么单独一个文件

`services/diff_service.py` 原先 1990 行、是一个 1917 行的类，贴着
`scripts/check_file_length.py --strict` 的 2000 行 ERROR。这里是最重的一块：
`_compare_dataframes` / `_detailed_dataframe_comparison` / `_smart_row_diff`（185 行）
构成「拿到两张表之后怎么产出变更」的主干，`_pair_columns` / `_resolve_key_columns`
决定列怎么对上、哪些列是关键列。

## mixin 形态与一处类名改写

类里除 `self.performance_stats` 外没有实例状态，抽成 mixin 后方法体一行不改。
例外只有一处：`_pair_columns` 里写了两遍 `DiffService._is_placeholder_column_name(...)`
—— 那个名字现在也住在本模块，改成引用本模块的 mixin 类名（`DiffService` 已经通过了
MRO 拿到它，但本模块 import `DiffService` 会成环）。这是全次拆分唯一改到的方法体。
"""

import re
from typing import Any, Dict

from services.excel_header_profiles import column_index


class DiffExcelCompareMixin:

    def _repair_inferred_cells(self, content: bytes, sheets: Dict[str, Any]) -> Dict[str, Any]:
        """把**被列级类型推断改写过的格子**改回原始单元格的字面量。

        为什么还需要这一步：`dtype=str` 防住的是「字面量被当成类型改写」（`'00123'`→123、
        `'TRUE'`→True…），但**列级**的类型推断发生在它之前 —— 一列里只要混进一个布尔，
        同列的数字与布尔就会互相转换，而 `dtype=str` 只是把转换后的结果转成字符串：

        * 数字变布尔：线上 6684 `硬直类型表.xlsx` 的「清理吸灵器吸住状态」列里有 16 个
          数字 `0`，读出来是 `False` —— 页面显示的取值根本不是文件里的那个；
        * 布尔变数字：同批语料里另一张表把文件里的 `TRUE` 显示成 `1`。
          两版之间只要有一版多了一个布尔，同一个格子就会一边显示 `0`、一边显示 `False`，
          于是报出一条**没人改过的变更**（变更确认平台上，这种假变更与漏报同样致命：
          评审者要去核对一个不存在的差异）。

        只改这些格子：判断依据是**原始单元格的类型**（openpyxl `data_only=True`，
        与 pandas 读的是同一份字节），其余格子一律保持 `dtype=str` 的读数不动。
        因此这一步不会改变「文本/数字/日期怎么显示」的任何既有口径，只把这两种
        被推断改写过的形态还原。代价是每份 Excel 多一遍原始扫描（read_only 流式，
        实测与 pandas 那一遍同量级；diff 有缓存，一个 (文件, 版本) 只付一次）。
        """
        if not sheets:
            return sheets
        try:
            import io

            import openpyxl

            workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        except Exception:
            # 读不出原始类型就保持原样：宁可维持既有读数，也不能让读取整份失败
            return sheets
        try:
            for sheet_name, df in sheets.items():
                if df is None or not hasattr(df, 'iat'):
                    continue
                try:
                    worksheet = workbook[sheet_name]
                except Exception:
                    continue
                columns = list(df.columns)
                for row_offset, raw_row in enumerate(worksheet.iter_rows(values_only=True)):
                    if row_offset == 0:
                        continue                    # 第 1 行是表头，已经成了列名
                    row_index = row_offset - 1      # pandas 的行号从 0 起
                    if row_index >= len(df):
                        break
                    for column_index, raw_value in enumerate(raw_row):
                        if column_index >= len(columns):
                            break
                        if isinstance(raw_value, bool):
                            # 布尔被读成 1/0 → 还原成布尔
                            current = str(df.iat[row_index, column_index])
                            if current in ('1', '0'):
                                df.iat[row_index, column_index] = 'True' if raw_value else 'False'
                        elif isinstance(raw_value, (int, float)):
                            # 数字被读成 True/False → 还原成数字
                            current = str(df.iat[row_index, column_index])
                            if current in self._BOOL_TEXTS:
                                df.iat[row_index, column_index] = str(raw_value)
        finally:
            try:
                workbook.close()
            except Exception:
                pass
        return sheets

    def _compare_excel_data(self, current_data: Dict, previous_data: Dict, file_path: str,
                            key_columns: str = None, header_rows: int = None,
                            header_name_row: int = None, marker_column: str = None) -> Dict[str, Any]:
        """比较Excel数据

        marker_column：「标记列」的列字母（如 `A`）。常规配表把这一列当**元数据**用 ——
        表头区放 `TYPE`/`DEFAULT`/`EXPORT` 这类标记，数据区放策划备注，两处都不是数据。
        配了它，该列在数据区就不产生任何变更（处理见 `_blank_marker_column`）。
        """

        header_count = self._header_row_count(header_rows)
        name_row = self._header_name_row(header_name_row, header_count)

        result = {
            'type': 'excel',
            'file_path': file_path,
            'sheets': {},
            'summary': {'added': 0, 'removed': 0, 'modified': 0, 'total': 0}
        }

        # 获取所有工作表名称
        all_sheets = set(current_data.keys()) | set(previous_data.keys())

        for sheet_name in all_sheets:
            current_df = current_data.get(sheet_name)
            previous_df = previous_data.get(sheet_name)

            sheet_diff = self._compare_dataframes(current_df, previous_df, sheet_name,
                                                  key_columns=key_columns,
                                                  header_rows=header_count,
                                                  header_name_row=name_row,
                                                  marker_column=marker_column)
            result['sheets'][sheet_name] = sheet_diff
            
            # 更新统计信息
            if 'stats' in sheet_diff:
                for key in ['added', 'removed', 'modified']:
                    result['summary'][key] += sheet_diff['stats'].get(key, 0)
        
        result['summary']['total'] = sum(result['summary'].values())
        
        return result

    @staticmethod
    def _blank_marker_column(df, marker_column):
        """把标记列的值清空 —— 它在数据区是备注，不是数据。

        ## 为什么是「清空」而不是「删掉这一列」

        删列会让列数少一个，`key_columns` 的**列号语义**当场错位：帮助页写明
        「列号从 1 开始，B 列就填 2」，删掉 A 列之后配了 `key_columns=2` 的仓库会突然
        按 C 列去认行 —— 而认错行的表现是「删一行 + 加一行」成对出现，不报错。
        清空则列序、列数、列名一个都不动。

        ## 为什么不是「保留显示、只跳过比较」

        那要把「显示用」和「比较用」两份数据分开往下传，改动会渗进 `_smart_row_diff`
        的比较循环，而收益只是「数据区还能看到备注」。备注的**变更**本来就不该出现在
        数据变更里；要在页面上看备注，表头区那一份照旧逐格显示（`_build_header_rows`
        读的是原始帧，不受这里影响）。

        已经全空时直接返回原帧，不走 `copy()` —— 每张表都复制一遍是白花的。
        """
        if df is None or not marker_column:
            return df
        index = column_index(marker_column)
        if index is None or df.shape[1] <= index:
            return df
        try:
            if not df[df.columns[index]].astype(str).str.strip().any():
                return df
        except Exception:  # noqa: BLE001 —— 判空失败就照常往下处理
            pass
        blanked = df.copy()
        blanked.iloc[:, index] = ""
        return blanked

    def _compare_dataframes(self, current_df, previous_df, sheet_name: str,
                            key_columns: str = None, header_rows: int = None,
                            header_name_row: int = None, marker_column: str = None) -> Dict[str, Any]:
        """比较两个DataFrame

        marker_column：标记列（见 `_compare_excel_data`）。**必须在三个分支之前处理** ——
        整表增/删那两条分支同样要按它把这一列排除掉，否则同一列备注在「改了一格」时
        不算变更、在「整表重建」时被算成 N 行变更，两边的口径对不上。

        header_rows 是「表头块占前几行」（含第 1 行的列名行，见 `_header_row_count`）。
        整个工作表增/删的两条分支同样要把表头行分出来 —— 否则新加一张三行表头的表，
        表头那两行会被算进「新增 N 行」的计数里。

        header_name_row 是名称行的物理行号（见 `_header_name_row`）。增/删工作表这两条
        分支也要按它取列名：不然同一张表在「整表新增」与「改了一格」两种提交里，
        列头会显示成两套名字（一边是第 1 行的标题占位名、一边是字段名）。
        """

        header_count = self._header_row_count(header_rows)
        name_row = self._header_name_row(header_name_row, header_count)

        # 标记列：数据区是策划备注、表头区是 TYPE/DEFAULT/EXPORT 这类标记，两处都不是数据。
        # **必须排在三个分支之前** —— 整表增/删那两条分支同样要排除它，否则同一列备注
        # 在「改了一格」时不算变更、在「整表重建」时被算成 N 行变更，两边口径对不上。
        current_df = self._blank_marker_column(current_df, marker_column)
        previous_df = self._blank_marker_column(previous_df, marker_column)

        if current_df is None and previous_df is None:
            return {'headers': [], 'rows': [], 'stats': {'added': 0, 'removed': 0, 'modified': 0}}

        if current_df is None:
            # 工作表被删除
            #
            # 行数据**必须留下** —— 这是评审者唯一能看到「删掉了什么」的地方。
            #
            # 原实现只留计数（`'rows': []`），于是两边渲染都不认：
            #   * 前端 `static/js/diff-handlers.js` 判断一个 sheet 有没有变更是看
            #     `rows.some(status ∈ added/removed/modified)` —— rows 为空即「无变更」；
            #   * 服务端 `templates/diff_partials/excel_diff.html` 也是逐 `rows` 渲染。
            # 结果是顶部统计照样写着「删除 232」，每一张表却都显示
            # 「工作表 X 没有数据或无变更」——**统计说有 232 行删除、正文一个字都看不到**，
            # 评审者只能盲签。（线上实例：`config/60_skill/角色属性表.xlsx` 一次删除
            # 232 行，正文只渲染出 1 行。）
            #
            # 与下面「新增工作表」分支对齐：那个分支一直是保留全部行的。
            # 负载变大的风险由既有的按体积截断机制兜底（`excel_diff_cache_service`
            # 的 MAX_DIFF_DATA_BYTES / truncated 标记，模板另有专门提示分支）。
            # 列名取「名称行」（配了的话）：整表删除时列头显示的也该是字段名。
            # 顺序要紧：改名必须排在取行**之前** —— `_dataframe_rows_with_index` 出来的
            # 行字典是按列名取值的，先取行再改名会让两边的键对不上。
            first_row = None
            if previous_df is not None:
                previous_df, first_row = self._rename_by_name_row(previous_df, name_row)
            headers = list(previous_df.columns) if previous_df is not None else []
            # 整行空白的行不发 —— 判空口径与比较分支（`_has_valid_data`）保持一致。
            # 不筛的话，表里本来就有的空行会被算成「删除行」：线上 6011 报
            # 「删除 5653 行」，其中大片是空行；6136 一张表报 1699 行、1686 行全空。
            filtered = [
                (row_number, row_data)
                for row_number, row_data in self._dataframe_rows_with_index(previous_df)
                if self._has_valid_data(row_data, headers)
            ] if previous_df is not None else []
            header_block, header_stats = self._build_header_rows(
                [], filtered, headers, header_count,
                name_row=name_row, first_rows=(None, first_row))
            rows = [
                {
                    'row_number': row_number,
                    'status': 'removed',
                    'data': row_data,
                }
                for row_number, row_data in filtered
                if row_number > header_count
            ]
            return self._with_header_block({
                'operation': 'deleted',
                'message': f'工作表 "{sheet_name}" 已被删除',
                'headers': headers,
                'rows': rows,
                'stats': {'added': 0, 'removed': len(rows), 'modified': 0}
            }, header_block, header_stats)

        if previous_df is None:
            # 新增工作表
            # 改名同样要排在取行之前（理由见上面「工作表被删除」那一支）。
            first_row = None
            current_df, first_row = self._rename_by_name_row(current_df, name_row)
            headers = list(current_df.columns)
            filtered = [
                (row_number, row_data)
                for row_number, row_data in self._dataframe_rows_with_index(current_df)
                if self._has_valid_data(row_data, headers)
            ]
            header_block, header_stats = self._build_header_rows(
                filtered, [], headers, header_count,
                name_row=name_row, first_rows=(first_row, None))
            rows = [
                {
                    'row_number': row_number,
                    'status': 'added',
                    'data': row_data
                }
                for row_number, row_data in filtered
                if row_number > header_count
            ]

            return self._with_header_block({
                'operation': 'added',
                'message': f'新增工作表 "{sheet_name}"',
                'headers': headers,
                'rows': rows,
                'stats': {'added': len(rows), 'removed': 0, 'modified': 0}
            }, header_block, header_stats)

        # 比较现有工作表
        return self._detailed_dataframe_comparison(current_df, previous_df,
                                                   key_columns=key_columns,
                                                   header_rows=header_count,
                                                   header_name_row=name_row)

    def _detailed_dataframe_comparison(self, current_df, previous_df, key_columns: str = None,
                                       header_rows: int = None,
                                       header_name_row: int = None) -> Dict[str, Any]:
        """详细比较两个DataFrame，支持行插入/删除的智能识别

        `header_name_row` 是名称行的物理行号：**列名取表头块里的第几行**（配 2 用于
        「第 1 行是大标题、第 2 行才是字段名」的表）。改名发生在这条链路的**最前面**
        （比 `_pair_columns` 与 `header_changes` 都早），否则「名称行改名」会被报成
        一串无名的假变更：配对按旧名认不出任何一列，列变更提示里还会同时出现旧名与
        新名。改名前的列名另存一份，供第 1 行的呈现用（见 `_plan_name_row`）。
        """
        # 列身份不能只看列名 —— 见 _pair_columns 的说明。
        # `header_rows`/`header_name_row` 到这里都已经是规范化过的值（`_compare_dataframes`
        # 传下来的），再规范化一次是幂等的，直接调用本方法的地方也不用自己先算。
        header_count = self._header_row_count(header_rows)
        name_row = self._header_name_row(header_name_row, header_count)
        rename_plan = self._plan_name_row(current_df, previous_df, name_row)
        raw_current = list(current_df.columns) if current_df is not None else []
        raw_previous = list(previous_df.columns) if previous_df is not None else []
        if rename_plan is not None:
            # `set_axis` 而不是 `df.columns = …`：传进来的帧是调用方（`_read_excel_data`
            # 的结果）持有的，就地改列名会让**同一次读取的第二遍比较**看到一份已经被
            # 改过名的帧 —— 那时「第 1 行的原文」会变成字段名，第 1 行也跟着显示错。
            current_df = current_df.set_axis(rename_plan['current'][0], axis=1)
            previous_df = previous_df.set_axis(rename_plan['previous'][0], axis=1)

        current_columns = list(current_df.columns) if current_df is not None else []
        previous_columns = list(previous_df.columns) if previous_df is not None else []

        pairs, current_only, previous_only = self._pair_columns(current_columns, previous_columns)

        header_changes = []
        first_rows = None
        if current_df is not None and previous_df is not None:
            # 把上一版的列名**按配对结果改写成当前列名**，再逐格比。
            # 不改名的话，一次列改名会让「旧名」和「新名」同时出现在列并集里，
            # 每一行都会多出「旧值→空」「空→新值」两处假变更（线上审计 6657）。
            mapped = list(previous_columns)
            for cur_idx, prev_idx in pairs:
                old_name, new_name = previous_columns[prev_idx], current_columns[cur_idx]
                if old_name != new_name and not self._is_placeholder_pair(old_name, new_name):
                    header_changes.append({
                        'change': 'renamed',
                        'column_index': cur_idx + 1,      # 从 1 开始，与 Excel 列序一致
                        'column': self._display_column_name(new_name, current_columns),
                        'old_name': self._display_column_name(old_name, previous_columns),
                        'new_name': self._display_column_name(new_name, current_columns),
                    })
                mapped[prev_idx] = new_name
            previous_df = previous_df.copy()
            previous_df.columns = mapped
            # 第 1 行的两版原文：`mapped` 正是上一版各列改叫的名字，所以它同时也是
            # 「上一版第 1 行的那一格该落在哪一列下」的对照表。
            #
            # **只有真的改过名才补**（`rename_plan`）：名称行还是第 1 行时，第 1 行就是
            # 列名本身，它的改动由 `header_changes` 表达、重名列也已由列头显示 ——
            # 再补一行出来会让未配置的仓库凭空多出表头块（载荷与今天不再逐字一致）。
            if rename_plan is not None:
                first_rows = (
                    {current_columns[i]: self._raw_first_row_cell(raw_current[i], raw_current)
                     for i in range(len(current_columns))},
                    {mapped[j]: self._raw_first_row_cell(raw_previous[j], raw_previous)
                     for j in range(len(mapped))},
                )
            for cur_idx in current_only:
                name = current_columns[cur_idx]
                if not self._is_reportable_column_change(name, current_columns, previous_columns):
                    continue
                header_changes.append({
                    'change': 'added',
                    'column_index': cur_idx + 1,
                    'column': self._display_column_name(name, current_columns),
                    'old_name': '',
                    'new_name': self._display_column_name(name, current_columns),
                })
            for prev_idx in previous_only:
                name = previous_columns[prev_idx]
                if not self._is_reportable_column_change(name, previous_columns, current_columns):
                    continue
                header_changes.append({
                    'change': 'removed',
                    'column_index': prev_idx + 1,
                    'column': self._display_column_name(name, previous_columns),
                    'old_name': self._display_column_name(name, previous_columns),
                    'new_name': '',
                })

        # 保持原始列顺序，优先使用当前文件的列顺序
        ordered_columns = list(current_columns)
        for prev_idx in previous_only:
            name = previous_columns[prev_idx]
            # 被删掉的列仍然要留在表里（它的旧值要能显示出来），但同名的不重复列。
            if name not in ordered_columns:
                ordered_columns.append(name)

        # 重新索引DataFrame以便比较，保持原始列顺序
        if current_df is not None:
            current_df = current_df.reindex(columns=ordered_columns, fill_value='')
        if previous_df is not None:
            previous_df = previous_df.reindex(columns=ordered_columns, fill_value='')

        # 使用智能diff算法处理行插入/删除
        result = self._smart_row_diff(current_df, previous_df, ordered_columns,
                                      key_columns=key_columns, header_rows=header_rows,
                                      name_row=name_row, first_rows=first_rows)
        if header_changes:
            result['header_changes'] = header_changes
        return result

    @staticmethod
    def _is_placeholder_column_name(name):
        """pandas 给「表头为空」的列起的占位名（`Unnamed: 7`）。

        占位名里带着**列的位置**，所以插/删一列之后，后面所有空表头列的占位名都会
        「变」一次 —— 那是位置造成的，不是有人改了列名。两个用途：

        * **不当锚点**：拿它按名字配对等于用位置配对，而位置正是插列之后最不可靠的
          东西（线上 6556 插一列，`Unnamed: 76` 会被错认成基线里的 `Unnamed: 75`，
          于是新列被报成「新增」的同时还漏掉一组列的归属）；
        * **不报列名变更**：两侧都是占位名时不必提示 —— 一次插列附赠一串
          `Unnamed: 75 → Unnamed: 76` 只会淹没真正的列变更。

        取值比较照旧（配对结果仍然参与逐格比），变的只是「谁是谁」和「报不报」。
        """
        return bool(re.fullmatch(r'\s*Unnamed: \d+(\.\d+)?\s*', str(name or '')))

    @classmethod
    def _is_placeholder_pair(cls, old_name, new_name):
        return cls._is_placeholder_column_name(old_name) and cls._is_placeholder_column_name(new_name)

    @classmethod
    def _is_reportable_column_change(cls, name, own_columns, other_columns) -> bool:
        """未配对的这一列，值不值得报成「新增列 / 删除列」。

        两个否决条件，都是**位置型伪名**在提示里的形态：

        1. **另一版的表头里有同名列** —— 那它就不是新增/删除，只是排在了别的位置
           （前面插了一列，后面整体后移）。线上 6685：插一个「出生点id」，平台报了
           13 条列变更，其中「完成进度增加」「标题」既被报新增又被报删除；而 6095 那种
           整段重复列名（`属性修改` / `属性修改.1` / …）的表上，同一列号被同时报
           新增与删除的有 38 处。同一张表同一个列名，两版都有 → 没有任何人改过列名。
        2. **占位名**（表头为空时 pandas 起的 `Unnamed: 7`）。名字里带的是列的位置，
           插一列就会让后面所有空表头列「换个名字」。与 `_pair_columns` 里不当锚点、
           不报改名同一条理由（见 `_is_placeholder_column_name`）——列还在、值还在，
           只是它没有名字，报「新增列 Unnamed: 11」对评审者没有任何信息量。

        注意这里只影响**列变更提示**：列仍然照常参与逐格比较（配对结果不变），
        没被配上的列的单元格值依旧会以「旧值→空 / 空→新值」的形式显示出来。
        """
        if cls._is_placeholder_column_name(name):
            return False
        return name not in set(other_columns)

    @classmethod
    def _display_column_name(cls, name, all_columns) -> str:
        """列变更提示里显示的列名：**去重后缀还原成原名 + 出现次序**。

        pandas 读到同名表头时，会把第 2 个起改名为 `X.1`、`X.2`…（配表里 `From`、
        `属性修改` 这类重复列很多，线上 6225 的表头里有 82 个 `From*`）。这个名字里
        带着**出现次序**，所以只要在前面插一个同名列，它后面所有 `X.k` 都会整体错位 ——
        直接报出来就是「新增列 From.79」，评审者会去找一个叫 `From.79` 的列，而表里
        根本没有这个名字的列。只有当一个列名就是另一个列名的 `.数字` 后缀、且**那个
        原名本身也在这份表头里**时才这样还原：表里真有一列叫 `X.N`（而没有 `X`）时，
        原样保留。
        """
        match = re.fullmatch(r'(.+)\.(\d+)', str(name or ''))
        if not match:
            return name
        base, occurrence = match.group(1), int(match.group(2))
        if base not in set(all_columns):
            return name
        return f'{base}（同名列第 {occurrence + 1} 个）'

    @staticmethod
    def _pair_columns(current_columns, previous_columns):
        """决定「当前的第 i 列」对应「上一版的第 j 列」。

        列身份**不能只看列名**：`header=0` 之后列名取自第 1 行，而第 1 行本身也是
        会被改的（表头改名、两行表头互换）。只按列名配对时，一次改名会让「旧名的列」
        与「新名的列」同时出现在列并集里，于是**每一行**都多出「旧值→空 / 空→新值」
        两处假变更，两列的值恰好相等时还会产出「零净变更」的幽灵行：
        线上审计 6657 只把两行表头互换（14 格），被报成 9 行、92 条变更，其中一行
        同时被报「新增」和「删除」；6408/6427 是同一形态。

        规则：
        1. 先按**同名列**配对（唯一、保持左右顺序不交叉）—— 最强的身份证据，也能把
           「中间插了一列」之后的所有列各自认回原位；
        2. 相邻两个锚点之间剩下的列，**只有两侧数量相等时才按顺序两两配对**：
           数量相等说明这一段的列是「改名」，数量不等说明有增删 —— 这时宁可按
           新增/删除报，也不要把一列的值挂到另一列名下（那正是审计里的「编造」形态）。
        """
        index = {}
        for j, name in enumerate(previous_columns):
            if DiffExcelCompareMixin._is_placeholder_column_name(name):
                continue
            index.setdefault(name, []).append(j)

        anchors = []
        used_previous = set()
        last_previous = -1
        for i, name in enumerate(current_columns):
            if DiffExcelCompareMixin._is_placeholder_column_name(name):
                continue        # 占位名带的是位置，不是身份 —— 不能当锚点
            for j in index.get(name, []):
                if j in used_previous or j <= last_previous:
                    continue
                anchors.append((i, j))
                used_previous.add(j)
                last_previous = j
                break

        anchored_current = {i for i, _ in anchors}
        pairs = list(anchors)
        bounds = [(-1, -1)] + anchors + [(len(current_columns), len(previous_columns))]
        for (prev_c, prev_p), (next_c, next_p) in zip(bounds, bounds[1:]):
            current_gap = [i for i in range(prev_c + 1, next_c) if i not in anchored_current]
            previous_gap = [j for j in range(prev_p + 1, next_p) if j not in used_previous]
            if current_gap and len(current_gap) == len(previous_gap):
                pairs.extend(zip(current_gap, previous_gap))

        paired_current = {i for i, _ in pairs}
        paired_previous = {j for _, j in pairs}
        current_only = [i for i in range(len(current_columns)) if i not in paired_current]
        previous_only = [j for j in range(len(previous_columns)) if j not in paired_previous]
        return sorted(pairs), current_only, previous_only

    def _resolve_key_columns(self, raw_value, columns):
        """把仓库配置的「关键列」解析成本次比较可用的列标签。

        配置口径见 `templates/help.html` 的「关键列」一节：列号**从 1 开始**、
        英文逗号分隔，如 `1,2,3`；「如果默认第二列（即 B 列）为 ID，可以设置关键列为 2」。
        读取用 `header=0`，列名取自第 1 行，但**左右顺序与 Excel 一致**，
        所以「列号 N」= 本次读出来的第 N 列（`columns[N-1]`）。
        也接受列字母（`A,B`）：界面上没写，但把 `B` 当成「列号 2」没有歧义。

        解析不出来的项一律忽略并记录：一个填错的关键列不该让整份 diff 失败 ——
        那会让用户既看不到差异，也不知道是配置问题。全部解析不出来时返回 []，
        调用方退回「未配置关键列」的行为。
        """
        if not raw_value:
            return []
        if not columns:
            return []
        labels = []
        seen = set()
        for piece in re.split(r'[,，;；\s]+', str(raw_value).strip()):
            if not piece:
                continue
            index = None
            if piece.isdigit():
                index = int(piece) - 1        # 配置从 1 开始，columns 从 0 开始
            elif re.fullmatch(r'[A-Za-z]{1,3}', piece):
                index = 0
                for char in piece.upper():
                    index = index * 26 + (ord(char) - ord('A') + 1)
                index -= 1
            if index is None or index < 0 or index >= len(columns):
                from utils.logger import log_print
                log_print(f"⚠️ 关键列配置项无效已忽略: {piece!r}（共 {len(columns)} 列）", 'EXCEL')
                continue
            label = columns[index]
            if label not in seen:
                seen.add(label)
                labels.append(label)
        return labels

    def _smart_row_diff(self, current_df, previous_df, all_columns, key_columns=None,
                        header_rows: int = None, name_row: int = None,
                        first_rows=None) -> Dict[str, Any]:
        """智能行差异算法，正确处理行插入、删除和修改

        key_columns 有值时先按关键列配对（见 `_resolve_key_columns`），
        配上的行不再参与相似度匹配 —— 否则一个「ID 从 5 改成 7」的行会被
        相似度匹配认成「同一行被修改」，而按关键列的契约那是「删一行 + 加一行」。

        header_rows 是表头块的行数（含第 1 行的列名行）。表头行**不参与**行匹配与
        数据行统计，单独成块 —— 见 `_build_header_rows` 里为什么要分出来。

        name_row/first_rows 见 `_plan_name_row`：名称行不是第 1 行时，第 1 行不在
        任何一帧里（pandas 把它读成了列名），只能由 `first_rows` 补进表头块。
        """
        # 转换为列表便于处理，保留原始行号
        current_rows_with_index = self._dataframe_rows_with_index(current_df)
        previous_rows_with_index = self._dataframe_rows_with_index(previous_df)

        # 过滤掉全NaN行，但保留原始行号
        current_filtered = []
        for orig_row_num, row_data in current_rows_with_index:
            if self._has_valid_data(row_data, all_columns):
                current_filtered.append((orig_row_num, row_data))

        previous_filtered = []
        for orig_row_num, row_data in previous_rows_with_index:
            if self._has_valid_data(row_data, all_columns):
                previous_filtered.append((orig_row_num, row_data))

        # 表头块（物理行 2..header_rows）先分出来，再拿剩下的数据行做匹配。
        # 顺序要紧：`total_rows_current` 之类的计数只该数数据行。
        header_count = self._header_row_count(header_rows)
        header_block, header_stats = self._build_header_rows(
            current_filtered, previous_filtered, all_columns, header_count,
            name_row=self._header_name_row(name_row, header_count), first_rows=first_rows)
        if header_count > 1:
            current_filtered = [item for item in current_filtered if item[0] > header_count]
            previous_filtered = [item for item in previous_filtered if item[0] > header_count]

        # 提取纯数据用于匹配
        current_rows = [row_data for _, row_data in current_filtered]
        previous_rows = [row_data for _, row_data in previous_filtered]

        # 大表常见场景：过滤后按顺序逐行等价，直接返回空差异
        rows_equal = False
        if len(current_rows) == len(previous_rows):
            rows_equal = True
            for idx, row_data in enumerate(current_rows):
                if not self._rows_equal(row_data, previous_rows[idx], all_columns):
                    rows_equal = False
                    break
        if rows_equal:
            return self._with_header_block({
                'rows': [],
                'stats': {
                    'total_rows_current': len(current_filtered),
                    'total_rows_previous': len(previous_filtered),
                    'added': 0,
                    'removed': 0,
                    'modified': 0
                },
                'headers': all_columns,
                'columns': all_columns
            }, header_block, header_stats)
        
        # 1) 关键列优先配对（仓库配了关键列时）。配表几乎都有 ID 列，
        #    按 ID 配对是唯一「不会把两行错配」的做法；相似度匹配只能猜。
        key_labels = self._resolve_key_columns(key_columns, all_columns)
        matches = []
        current_matched = set()
        previous_matched = set()
        if key_labels:
            key_pairs, current_duplicates, previous_duplicates = self._match_rows_by_key(
                current_rows, previous_rows, key_labels)
            for current_idx, previous_idx in key_pairs:
                similarity = self._calculate_row_similarity(
                    current_rows[current_idx], previous_rows[previous_idx], all_columns)
                matches.append({
                    'type': 'match',
                    'matched_by': 'key',
                    'current_idx': current_idx,
                    'previous_idx': previous_idx,
                    'similarity': similarity,
                })
                current_matched.add(current_idx)
                previous_matched.add(previous_idx)
            # 谁能进相似度阶段：键不可用的行（表头/汇总这类没有 ID 的行），
            # 以及键重复的行（那组配置不足以唯一标识一行，只能按内容猜）。
            similarity_current = [
                idx for idx, row in enumerate(current_rows)
                if self._row_key(row, key_labels) is None
                or self._row_key(row, key_labels) in current_duplicates
                or self._row_key(row, key_labels) in previous_duplicates
            ]
            similarity_previous = [
                idx for idx, row in enumerate(previous_rows)
                if self._row_key(row, key_labels) is None
                or self._row_key(row, key_labels) in current_duplicates
                or self._row_key(row, key_labels) in previous_duplicates
            ]
        else:
            similarity_current = list(range(len(current_rows)))
            similarity_previous = list(range(len(previous_rows)))

        if similarity_current and similarity_previous:
            matches.extend(self._find_row_matches(
                [current_rows[i] for i in similarity_current],
                [previous_rows[j] for j in similarity_previous],
                all_columns,
                index_offset_current=similarity_current,
                index_offset_previous=similarity_previous,
            ))

        matches.sort(key=lambda x: x['current_idx'])

        rows = []
        
        # 处理匹配的行
        for match in matches:
            current_idx = match['current_idx']
            previous_idx = match['previous_idx']
            similarity = match['similarity']
            
            current_matched.add(current_idx)
            previous_matched.add(previous_idx)
            
            current_row = current_rows[current_idx]
            previous_row = previous_rows[previous_idx]
            
            # 使用当前行在过滤后列表中的原始行号
            orig_row_num = current_filtered[current_idx][0]
            # 匹配到的**上一版本**那一行的行号。插/删一行之后，同一个逻辑行在两版里
            # 的行号会不同（线上实测：当前第 11 行对应上一版第 12 行）—— 只报当前行号
            # 的话，评审者拿它回旧文件里核对就会整体错一行。
            previous_row_num = previous_filtered[previous_idx][0]

            if similarity < 1.0:
                # 计算具体的字段变更
                cell_changes = self._row_cell_changes(current_row, previous_row, all_columns)

                rows.append({
                    'row_number': orig_row_num,
                    'previous_row_number': previous_row_num,
                    'status': 'modified',
                    'data': current_row,
                    'cell_changes': cell_changes
                })
        
        # 处理新增的行（在当前版本中但未匹配）
        for i, (orig_row_num, row_data) in enumerate(current_filtered):
            if i not in current_matched:
                rows.append({
                    'row_number': orig_row_num,
                    'status': 'added',
                    'data': row_data
                })
        
        # 处理删除的行（在前一版本中但未匹配）
        for i, (orig_row_num, row_data) in enumerate(previous_filtered):
            if i not in previous_matched:
                rows.append({
                    'row_number': orig_row_num,
                    'status': 'removed',
                    'data': row_data
                })
        
        # 按行号排序
        rows.sort(key=lambda x: x['row_number'])
        
        # 统计信息
        stats = {
            'total_rows_current': len(current_filtered),
            'total_rows_previous': len(previous_filtered),
            'added': len([r for r in rows if r['status'] == 'added']),
            'removed': len([r for r in rows if r['status'] == 'removed']),
            'modified': len([r for r in rows if r['status'] == 'modified'])
        }
        
        return self._with_header_block({
            'rows': rows,
            'stats': stats,
            'headers': all_columns,
            'columns': all_columns
        }, header_block, header_stats)
