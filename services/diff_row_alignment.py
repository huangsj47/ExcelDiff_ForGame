"""行怎么配上：键、相似度、哈希索引、位置匹配与 DP 对齐。

## 为什么单独一个文件

`services/diff_service.py` 原先 1990 行，贴着 `scripts/check_file_length.py --strict`
的 2000 行 ERROR。这里是「两张表的行怎么对应起来」的全部判据：`_normalize_value`
决定什么算同一个值、`_calculate_row_similarity` 决定多像算同一行、
`_find_position_based_matches` / `_align_segment_dp` 决定偏移与插入删除怎么对齐。
**判断「这张表报得准不准」时只需要看这一个文件** —— 这是它单独存在的理由。

类的 `ROW_SIMILARITY_THRESHOLD` 等阈值参数仍在 `diff_service.py`：它们同时被比较引擎
用到，只留一份。本模块的方法通过 `self.` 取用（mixin 在 MRO 上，解析照旧）。
"""

import pandas as pd


class DiffRowAlignmentMixin:

    def _row_key(self, row, key_columns):
        """取一行的关键列取值。任一关键列为空 → 这一行没有身份，返回 None。

        取的是 `_normalize_value` 归一后的值（与 `_values_equal` 同一处口径），
        所以 `null` 字面量与真空白不会因为写法不同被当成两个键。
        """
        values = []
        for column in key_columns:
            value = self._normalize_value(row.get(column, ''))
            if value is None:
                return None
            values.append(value)
        return tuple(values)

    def _has_valid_data(self, row_data, columns):
        """检查行是否包含有效数据（**只有真正的空才算空**）

        判空口径与 `_normalize_value` 共用：None / NaN / NaT / pd.NA / 空字符串算空；
        文本 `null` / `None` / `nan` / `<NA>` / `N/A` 和空白串（`' '`）都是**取值**。
        与读取阶段（dtype=str, keep_default_na=False）保持同一口径：表里怎么写就怎么比。

        历史行为（已修）：这里曾用 `str(val).strip().lower()` 与黑名单
        `['', 'nan', 'none', 'null', '<na>']` 判空 —— 与同文件 `_normalize_value` 的口径
        正好相反，于是同一行数据在比较层算「有值」、在过滤层算「空行」。
        后果不是「少显示一行」，而是**整行在比较之前就被丢掉**：当一行的每个格子都
        落在黑名单里时（一行 `null`、一行空格），这行的任何改动都不报，
        summary 是 `{'added': 0, 'removed': 0, 'modified': 0}` → 界面显示「没有变更」，
        连 `_read_excel_data` 注释里承诺的「清空单元格不会被吃掉」在整行字面量时也失效。
        回归保护见 tests/test_diff_service_fidelity.py。
        """
        for col in columns:
            if self._normalize_value(row_data.get(col, '')) is not None:
                return True
        return False

    def _filter_nan_rows(self, rows, columns):
        """过滤掉全空行

        与 `_has_valid_data` 共用同一处判空口径 —— 历史上这里是黑名单的第二个副本
        （同样把文本 `null` / 空白串当空），两处必须一起改，不能只改一个。
        """
        return [row for row in rows if self._has_valid_data(row, columns)]

    def _rows_equal(self, row1, row2, columns):
        """检查两行是否完全相等，改进空值处理"""
        normalize = self._normalize_value
        for col in columns:
            norm_val1 = normalize(row1.get(col, ''))
            norm_val2 = normalize(row2.get(col, ''))
            
            if norm_val1 != norm_val2:
                return False
        
        return True

    def _values_equal(self, val1, val2):
        """检查两个值是否相等，改进空值处理"""
        return self._normalize_value(val1) == self._normalize_value(val2)

    @staticmethod
    def _normalize_value(val):
        """标准化单元格值：**只**把真正的空值归一为 None，不改写任何字面量。

        这是「什么算变更」的总闸门 —— `_values_equal` / `_calculate_row_similarity`
        / `_rows_equal` 全都走它。

        历史行为（已修）：这里曾把 `'nan' / 'none' / 'null' / '<na>'` 这些
        **文本**也判成空值，并对返回值做 `strip()`。两个后果都是静默漏审：
          * 文本 `null` 与真空白被判「相等」→ 「NULL 改成空」不报变更；
          * `'  x  '` 与 `'x'` 被判「相等」→ 首尾空格变更不报变更。
        配表里用 `null` / `None` 表示「无掉落 / 无引用」极常见，所以这不是理论问题。

        现在的口径与读取阶段（dtype=str, keep_default_na=False）一致：**表里怎么写就怎么比**。
        只有 None / NaN / NaT / pd.NA / 空字符串算空；空白串（如 `'   '`）算**有值**，
        因为「有空格」与「没有内容」在配表里是两种不同的写法。
        """
        if val is None:
            return None
        try:
            # pd.isna 对标量返回 np.bool_（`x is True` 会失败），必须显式 bool()；
            # 入参是列表/数组时返回数组，bool() 抛 ValueError → 落到下面按字符串处理。
            if bool(pd.isna(val)):
                return None
        except (TypeError, ValueError):
            pass
        val_str = str(val)
        if val_str == '':
            return None
        return val_str

    def _calculate_row_hash(self, row, columns):
        """计算行的哈希值用于快速匹配
        
        改进点：
        1. 使用全部列计算哈希，而非仅前5列，减少碰撞
        2. 空行返回唯一标记而非0，避免所有空行互相错误匹配
        """
        hash_values = []
        for col in columns:  # 使用全部列
            val = row.get(col, '')
            if val is not None and str(val).strip():
                hash_values.append(str(val).strip().lower())
        
        if not hash_values:
            # 空行返回基于id的唯一标记，避免所有空行互相匹配
            return id(row)
        
        return hash(tuple(hash_values))

    def _calculate_row_similarity(self, row1, row2, columns):
        """计算两行之间的相似度，改进NaN和空值处理"""
        total_cols = len(columns)
        if total_cols == 0:
            return 0
        
        normalize = self._normalize_value
        matching_cols = 0
        for col in columns:
            norm_val1 = normalize(row1.get(col, ''))
            norm_val2 = normalize(row2.get(col, ''))
            
            if norm_val1 is None and norm_val2 is None:
                matching_cols += 1  # 都是空值，认为匹配
            elif norm_val1 == norm_val2:
                matching_cols += 1  # 值相同
        
        return matching_cols / total_cols

    def _quick_similarity_check(self, row1, row2, columns):
        """快速相似度预检，避免不必要的详细计算

        未配置关键列时的兜底启发式：用前 DEFAULT_KEY_COLUMN_COUNT 列做预检。
        配了关键列的仓库不走这里 —— 那时行身份由 `_match_rows_by_key` 决定，
        相似度阶段只处理键不可用的行（表头/汇总这类），再拿「前 3 列」当键没有意义。
        """
        key_columns = columns[:min(self.DEFAULT_KEY_COLUMN_COUNT, len(columns))]
        
        matching_key_cols = 0
        for col in key_columns:
            val1 = row1.get(col, '')
            val2 = row2.get(col, '')
            
            if self._values_equal(val1, val2):
                matching_key_cols += 1
        
        # 至少有2个关键列匹配才通过预检（关键列不足2个时降级为至少1个）
        min_required = min(2, len(key_columns))
        if matching_key_cols >= min_required:
            return True
            
        # 特殊检查：如果第一列（通常是ID列）完全相同，也进入详细计算
        if len(columns) > 0:
            first_col = columns[0]
            val1 = str(row1.get(first_col, '')).strip()
            val2 = str(row2.get(first_col, '')).strip()
            
            if val1 and val2 and val1 == val2:
                return True
        
        return False

    def _match_rows(self, current_rows, previous_rows, columns):
        """认没变的行 → 切段 → 段内对齐，返回匹配列表。"""
        identical = self._match_identical_rows(current_rows, previous_rows, columns)
        anchors = self._pick_monotonic_anchors(identical)
        matched_current = {m['current_idx'] for m in identical}
        matched_previous = {m['previous_idx'] for m in identical}

        matches = list(identical)
        matches.extend(self._align_segments(current_rows, previous_rows, columns,
                                            anchors, matched_current, matched_previous))
        matches.sort(key=lambda m: (m['current_idx'], m['previous_idx']))
        return matches

    def _match_identical_rows(self, current_rows, previous_rows, columns):
        """配对两版里**完全相等**的行（任意位置）。

        哈希只是预筛（「非空值小写去空白」的拼串有碰撞），最终以逐格相似度 == 1.0 确认。
        这些行不产生任何差异输出，但它们是后面切段对齐的锚点。
        """
        index = {}
        for j, row in enumerate(previous_rows):
            index.setdefault(self._calculate_row_hash(row, columns), []).append(j)

        used_previous = set()
        matches = []
        for i, row in enumerate(current_rows):
            for j in index.get(self._calculate_row_hash(row, columns), []):
                if j in used_previous:
                    continue
                if self._calculate_row_similarity(row, previous_rows[j], columns) == 1.0:
                    matches.append({
                        'type': 'match',
                        'matched_by': 'identical',
                        'current_idx': i,
                        'previous_idx': j,
                        'similarity': 1.0,
                    })
                    used_previous.add(j)
                    break
        return matches

    def _match_rows_by_key(self, current_rows, previous_rows, key_columns):
        """按关键列配对两版的行，返回 [(current_idx, previous_idx), ...]。

        只在**键在两版里都唯一**时配对：键重复就说明这组配置并不能唯一标识一行
        （配表里 类型+id 才是唯一键的情况很常见），这时硬配对只会把 A 行配到 B 行，
        所以重复键的行留给相似度阶段处理。

        也返回「哪些行的键是可用的」，供调用方决定谁可以进入相似度阶段。
        """
        def _index(rows):
            mapping = {}
            duplicates = set()
            for index, row in enumerate(rows):
                key = self._row_key(row, key_columns)
                if key is None:
                    continue
                if key in mapping:
                    duplicates.add(key)
                else:
                    mapping[key] = index
            return mapping, duplicates

        current_map, current_duplicates = _index(current_rows)
        previous_map, previous_duplicates = _index(previous_rows)

        pairs = []
        for key, current_idx in current_map.items():
            if key in current_duplicates or key in previous_duplicates:
                continue
            previous_idx = previous_map.get(key)
            if previous_idx is None:
                continue
            pairs.append((current_idx, previous_idx))
        pairs.sort()
        return pairs, current_duplicates, previous_duplicates

    @staticmethod
    def _remap_match_indices(matches, offsets_current, offsets_previous):
        """把「子集下标」的匹配结果映射回完整行列表的下标。

        关键列阶段已经把一部分行配掉了，相似度阶段只在剩下的行上跑；
        返回时必须还原成完整列表的下标，否则 modified/added/removed 会挂到别的行上。
        """
        remapped = []
        for match in matches:
            current_idx = match['current_idx']
            previous_idx = match['previous_idx']
            if current_idx >= len(offsets_current) or previous_idx >= len(offsets_previous):
                continue
            new_match = dict(match)
            new_match['current_idx'] = offsets_current[current_idx]
            new_match['previous_idx'] = offsets_previous[previous_idx]
            remapped.append(new_match)
        remapped.sort(key=lambda m: m['current_idx'])
        return remapped

    def _find_row_matches(self, current_rows, previous_rows, columns,
                          index_offset_current=None, index_offset_previous=None):
        """行匹配入口：**小表大表同一条路径**。

        1. 先认出完全没变的行（哈希预筛 + 逐格确认，允许出现在任意位置）——
           这些行不需要报变更，同时充当对齐的锚点；
        2. 以锚点把两侧切成若干段，段内独立对齐（DP，超规模退回带位移的贪心）。

        为什么按段对齐：插入了 40 行之后，被改动的那一行对应的前一版行号偏移了 40，
        固定窗口（无论 ±10 还是 ±10%）都够不着 —— 那一片「插入 + 改动」会被算成
        一大片「删除 + 新增」。按锚点切段后，位移只影响它所在的那一段，
        段内 DP 能正确地把「插入的 40 行」与「改动的 1 行」分开。

        index_offset_*：传入的是**子集**时（关键列阶段已经配掉的行不再参与相似度
        匹配），用它在返回前把下标映射回完整行列表的下标 —— 否则结果会指到错误的行上。
        """
        offsets_current = (list(index_offset_current) if index_offset_current is not None
                           else list(range(len(current_rows))))
        offsets_previous = (list(index_offset_previous) if index_offset_previous is not None
                            else list(range(len(previous_rows))))

        matches = self._match_rows(current_rows, previous_rows, columns)
        return self._remap_match_indices(matches, offsets_current, offsets_previous)

    @staticmethod
    def _pick_monotonic_anchors(matches):
        """从「完全相等」的配对里取一个不交叉的子序列当锚点。

        内容相同的行可能被配到任意位置（两行互换时就会交叉），交叉的锚点无法用来
        切段；被剔掉的配对**仍然算已匹配**，只是不参与切段。
        做法：按 previous_idx 排序后，对 current_idx 求最长递增子序列。
        """
        from bisect import bisect_left
        ordered = sorted(matches, key=lambda m: (m['previous_idx'], m['current_idx']))
        tails = []
        tails_idx = []
        prev_link = [-1] * len(ordered)
        for pos, match in enumerate(ordered):
            value = match['current_idx']
            slot = bisect_left(tails, value)
            if slot == len(tails):
                tails.append(value)
                tails_idx.append(pos)
            else:
                tails[slot] = value
                tails_idx[slot] = pos
            prev_link[pos] = tails_idx[slot - 1] if slot > 0 else -1
        if not tails_idx:
            return []
        chain = []
        cursor = tails_idx[-1]
        while cursor != -1:
            chain.append(ordered[cursor])
            cursor = prev_link[cursor]
        chain.reverse()
        return chain

    def _align_segments(self, current_rows, previous_rows, columns,
                        anchors, matched_current, matched_previous):
        """以锚点切段，段内对齐（DP；超规模退回带位移的贪心）。"""
        matches = []
        ordered = sorted(anchors, key=lambda m: m['current_idx'])
        bounds = [(-1, -1)]
        bounds.extend((m['current_idx'], m['previous_idx']) for m in ordered)
        bounds.append((len(current_rows), len(previous_rows)))
        for (prev_c, prev_p), (next_c, next_p) in zip(bounds, bounds[1:]):
            cur_lo, cur_hi = prev_c + 1, next_c
            prev_lo, prev_hi = prev_p + 1, next_p
            if cur_lo >= cur_hi or prev_lo >= prev_hi:
                continue
            cur_idx = [i for i in range(cur_lo, cur_hi) if i not in matched_current]
            prev_idx = [j for j in range(prev_lo, prev_hi) if j not in matched_previous]
            if not cur_idx or not prev_idx:
                continue
            if len(cur_idx) * len(prev_idx) <= self.ALIGN_SEGMENT_MAX_CELLS:
                matches.extend(self._align_segment_dp(
                    current_rows, previous_rows, columns, cur_idx, prev_idx))
            else:
                # 段太大（例如整表重排）：退回带位移的贪心，代价有界
                compact_current = [current_rows[i] for i in cur_idx]
                compact_previous = [previous_rows[j] for j in prev_idx]
                greedy = self._find_position_based_matches(
                    compact_current, compact_previous, columns, set(), set())
                for match in greedy:
                    matches.append({
                        'type': 'match',
                        'matched_by': 'greedy',
                        'current_idx': cur_idx[match['current_idx']],
                        'previous_idx': prev_idx[match['previous_idx']],
                        'similarity': match['similarity'],
                    })
        return matches

    def _align_segment_dp(self, current_rows, previous_rows, columns, cur_idx, prev_idx):
        """段内对齐：把「哪些行配成一对」建成 DP。

        dp[i][j] = 前 i 条当前行与前 j 条前一版行能配出的最大得分；
        配对得分 = 1 + 相似度（相似度必须超过阈值才允许配对），跳过不得分。
        于是优先「配出最多的对数」，同分时偏好相似度更高的配对 —— 插在中间的行
        会被当作跳过（新增），而不是把后面的行一一错配。

        规模由 `ALIGN_SEGMENT_MAX_CELLS` 兜住（本函数只在段内行数乘积不超限时调用）。
        """
        a, b = len(cur_idx), len(prev_idx)
        # 阈值是「**最低**相似度」，所以取等号：五列里改两格恰好是 0.6，
        # 用严格大于会把它拒配，同一行的改写就降级成「删一行 + 加一行」
        # （线上审计 6549：`{19, M4-折纸房-19旋转金币}` → `{9, M4-折纸房-旋转金币}`
        # 被报成 added 1 + removed 1，而它其实是 1 行修改）。
        threshold = self.ROW_SIMILARITY_THRESHOLD
        similarities = {}
        dp = [[0.0] * (b + 1) for _ in range(a + 1)]
        for i in range(1, a + 1):
            current_row = current_rows[cur_idx[i - 1]]
            for j in range(1, b + 1):
                similarity = self._calculate_row_similarity(
                    current_row, previous_rows[prev_idx[j - 1]], columns)
                similarities[(i, j)] = similarity
                best = max(dp[i - 1][j], dp[i][j - 1])
                if similarity >= threshold:
                    best = max(best, dp[i - 1][j - 1] + 1.0 + similarity)
                dp[i][j] = best

        matches = []
        i, j = a, b
        while i > 0 and j > 0:
            similarity = similarities[(i, j)]
            if similarity >= threshold and abs(
                    dp[i][j] - (dp[i - 1][j - 1] + 1.0 + similarity)) < 1e-9:
                matches.append({
                    'type': 'match',
                    'matched_by': 'aligned',
                    'current_idx': cur_idx[i - 1],
                    'previous_idx': prev_idx[j - 1],
                    'similarity': similarity,
                })
                i -= 1
                j -= 1
            elif dp[i - 1][j] >= dp[i][j - 1]:
                i -= 1
            else:
                j -= 1
        matches.reverse()
        return matches

    def _find_position_based_matches(self, current_rows, previous_rows, columns,
                                     used_previous, used_current, last_matched_previous=-1):
        """按位置（带**单调约束**）配对剩下的行，识别「被修改的行」。

        单调约束：当前行按顺序扫，配到的前一版行号必须**严格递增**
        （`last_matched_previous` 只增不减），搜索区间也从它之后开始。

        为什么必须单调：行在配表里是有序的，真实对应关系不会交叉。原实现允许
        任意配对（每个当前行都在 ±range 内独立找最相似的未用行），于是出现
        「第 10 行被配给了第 21 行的对手方」这种交叉 —— 线上审计 5988 报的那条
        「修改」里 old 取自 Excel 第 21 行、new 取自第 10 行，就是交叉配对的产物；
        而真正的对手方被别的行抢走后，只剩「未匹配」可走，整块变更还会被压成一行。
        贪心 + 无约束的配对结果也依赖扫描顺序，同一份表换个行序结论就变。

        另外跟踪**累计位移** `delta`：插入了 30 行之后，第 i 行对应的前一版行是
        i+30 而不是 i，窗口必须跟着位移走，否则整块「插入 + 改动」会被算成
        一大片「删除 + 新增」。delta 每次配对成功后更新为 `j - i`。

        搜索半径 = max(ROW_POSITION_SEARCH_MIN, 表大小 × ROW_POSITION_SEARCH_RATIO)，
        阈值 = ROW_SIMILARITY_THRESHOLD（与其它路径共用同一套）。
        """
        matches = []

        # 自适应搜索范围：至少 ROW_POSITION_SEARCH_MIN 行，最多为数据集大小的 ROW_POSITION_SEARCH_RATIO
        data_size = max(len(current_rows), len(previous_rows))
        search_range = max(self.ROW_POSITION_SEARCH_MIN,
                           int(data_size * self.ROW_POSITION_SEARCH_RATIO))

        # 累计位移：当前行 i 对应的前一版行 ≈ i + delta
        delta = 0

        # 对于未匹配的当前行，尝试与相近位置的前一版本行匹配
        for i, current_row in enumerate(current_rows):
            if i in used_current:
                continue

            # 单调：只能往 last_matched_previous 之后找；窗口跟着累计位移走
            center = i + delta
            start_idx = max(last_matched_previous + 1, center - search_range, 0)
            end_idx = min(len(previous_rows), center + search_range + 1)

            best_match = None
            best_score = 0

            for j in range(start_idx, end_idx):
                if j in used_previous:
                    continue

                # 快速预检：跳过明显不相关的行
                if not self._quick_similarity_check(current_row, previous_rows[j], columns):
                    continue

                score = self._calculate_row_similarity(current_row, previous_rows[j], columns)

                # 与其它路径同一个阈值（含等号：阈值是「最低相似度」）
                if score >= self.ROW_SIMILARITY_THRESHOLD and score > best_score:
                    best_score = score
                    best_match = j

            if best_match is not None:
                matches.append({
                    'type': 'modified',
                    'current_idx': i,
                    'previous_idx': best_match,
                    'similarity': best_score
                })
                used_previous.add(best_match)
                used_current.add(i)
                last_matched_previous = best_match
                delta = best_match - i      # 后面几行的窗口跟着这次配对整体平移

        return matches

    def _row_cell_changes(self, current_row, previous_row, columns):
        """一行之内逐列的取值变更（判等口径统一走 `_values_equal`）。"""
        changes = []
        for col in columns:
            old_val = previous_row.get(col, '')
            new_val = current_row.get(col, '')
            if not self._values_equal(old_val, new_val):
                changes.append({
                    'column': col,
                    'old_value': old_val,
                    'new_value': new_val
                })
        return changes
