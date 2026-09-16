import os
import mimetypes
import base64
from typing import Dict, Any, Optional, Tuple
import difflib
import re
import pandas as pd
import numpy as np

class DiffService:
    """统一的文件差异服务，支持4种文件类型的diff处理"""
    
    # 文件类型定义
    TEXT_EXTENSIONS = {
        '.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.yaml', '.yml',
        '.md', '.rst', '.c', '.cpp', '.h', '.hpp', '.java', '.cs', '.php', '.rb', '.go',
        '.rs', '.lua', '.sql', '.sh', '.bat', '.ps1', '.ini', '.cfg', '.conf',
        '.log', '.properties', '.gitignore', '.dockerfile'
    }
    
    EXCEL_EXTENSIONS = {
        '.xls', '.xlsx', '.xlsm', '.xlsb', '.ods'
    }
    
    CSV_EXTENSIONS = {
        '.csv', '.tsv'
    }
    
    IMAGE_EXTENSIONS = {
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.tiff', '.ico',
        '.heic', '.heif', '.raw', '.psd'
    }

    # ------------------------------------------------------------------
    #  行匹配参数 —— **小表与大表共用同一套**
    #
    #  历史行为（已修）：同一次比较会按表的行数走两条不同的路径，
    #  `len(rows) > 100` 走 `_fast_row_matching`（哈希阈值 0.85、位置匹配 0.5），
    #  否则走小表路径（阈值 0.6）。于是**同一个改动、同一份表，仅仅因为行数跨过
    #  100 行，结论就不同**：既不可复现，也让「为什么这张表准、那张表不准」无法解释
    #  （线上审计：5988 报 3 行而 git 是 456 行的表，正是大表路径）。
    #
    #  哈希索引保留，但它的职责收窄成「认出完全没变的行」——哈希是「非空值小写去空白」
    #  的拼串，本来就有碰撞，用它去接受 0.85 相似度的行等于让碰撞决定配对结果。
    #  变了多少一律交给位置阶段，用同一个阈值判定。
    # ------------------------------------------------------------------
    ROW_SIMILARITY_THRESHOLD = 0.6      # 认定为「同一行被修改」的最低相似度
    LARGE_TABLE_ROW_THRESHOLD = 100     # 超过此行数改用哈希加速（只是加速，不换口径）
    ROW_POSITION_SEARCH_MIN = 10        # 位置匹配的最小搜索半径
    ROW_POSITION_SEARCH_RATIO = 0.1     # 搜索半径 = 表大小 × 该比例（取两者较大值）
    DEFAULT_KEY_COLUMN_COUNT = 3        # 未配置关键列时，快速预检用前 N 列

    def __init__(self):
        self.performance_stats = {
            'text_diff_time': 0,
            'excel_diff_time': 0,
            'image_diff_time': 0,
            'binary_diff_time': 0
        }
    
    def get_file_type(self, file_path: str) -> str:
        """根据文件扩展名判断文件类型"""
        ext = os.path.splitext(file_path.lower())[1]
        
        if ext in self.EXCEL_EXTENSIONS:
            return 'excel'
        elif ext in self.CSV_EXTENSIONS:
            return 'excel'  # CSV也作为Excel处理
        elif ext in self.TEXT_EXTENSIONS:
            return 'text'
        elif ext in self.IMAGE_EXTENSIONS:
            return 'image'
        else:
            return 'binary'
    
    def process_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None,
                     key_columns: str = None) -> Dict[str, Any]:
        """处理文件差异，根据文件类型选择合适的处理方式。

        key_columns：仓库上配置的「关键列」（`Repository.key_columns`，列号从 1 开始、
        英文逗号分隔，如 `1,2,3`）。配了它，行匹配就按这些列的值认「同一行」——
        这是帮助文档已经写明的契约（`templates/help.html` 的「关键列」一节），
        但引擎历史上从来没读过这个配置，只按前 3 列的相似度猜配对，
        于是把不同的行配成一条「修改」，报出来的改前值是另一行的
        （线上审计：5988 报的行 9 里 old 取自第 21 行、new 取自第 10 行）。
        """
        file_type = self.get_file_type(file_path)

        try:
            if file_type == 'text':
                return self._process_text_diff(file_path, current_content, previous_content)
            elif file_type == 'excel':
                return self._process_excel_diff(file_path, current_content, previous_content,
                                                key_columns=key_columns)
            elif file_type == 'image':
                return self._process_image_diff(file_path, current_content, previous_content)
            else:
                return self._process_binary_diff(file_path, current_content, previous_content)
        except Exception as e:
            return {
                'type': 'error',
                'file_path': file_path,
                'error': str(e),
                'message': f'处理文件差异时发生错误: {str(e)}'
            }
    
    def process_deleted_file(self, file_path: str, previous_content: bytes) -> Dict[str, Any]:
        """整份文件被删除时的差异：基线的**每一张工作表**都按「已删除」处理。

        为什么不复用 `process_diff(path, None, previous_content)`：通用路径上
        `current_content=None` 有两种彼此相反的语义 ——
          * 「文件在这个提交里不存在」（真删除）：要把被删掉的内容全渲染出来；
          * 「从 VCS 读内容失败」（git 超时、路径写错、仓库没同步）：**必须报错**，
            渲染成「全表删除」等于让评审者把一次读取失败当成一次真实的删除确认掉。
        通用路径对空内容抛错正是为了守住后者，所以「删除」只能由调用方**显式声明**，
        绝不靠 None 推断（`services/vcs_content_service.py::get_deleted_file_diff_data`
        就是这样做的：它已经确认了 `commit.operation == 'D'`，并且拿到的是真实基线的字节）。

        实现上与「工作表被删除」共用一条路径：current 传 `{}` ⇒ 每张表都走
        `_compare_dataframes` 的 deleted 分支，行数据（status='removed' + data）被保留。
        """
        if not previous_content:
            # 基线的字节也拿不到：给一个诚实的空结果，让上层显示「无法获取差异」，
            # 而不是编造一份「没有任何内容的删除」。
            return {
                'type': 'excel',
                'file_path': file_path,
                'sheets': {},
                'summary': {'added': 0, 'removed': 0, 'modified': 0, 'total': 0},
            }
        previous_data = self._read_excel_data(previous_content, file_path)
        return self._compare_excel_data({}, previous_data, file_path)

    def _process_text_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None) -> Dict[str, Any]:
        """处理文本文件差异"""
        import time
        start_time = time.time()
        
        try:
            # 尝试解码文本内容
            current_text = self._decode_text(current_content)
            previous_text = self._decode_text(previous_content) if previous_content else ""
            
            # 生成统一格式的diff
            diff_lines = list(difflib.unified_diff(
                previous_text.splitlines(keepends=True),
                current_text.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                lineterm=""
            ))
            
            # 解析diff为结构化数据
            hunks = self._parse_unified_diff_lines(diff_lines)
            
            # 计算统计信息
            stats = self._calculate_text_stats(hunks)
            
            self.performance_stats['text_diff_time'] += time.time() - start_time
            
            return {
                'type': 'text',
                'file_path': file_path,
                'hunks': hunks,
                'stats': stats,
                'raw_diff': ''.join(diff_lines),
                'current_content': current_text,
                'previous_content': previous_text
            }
            
        except Exception as e:
            return {
                'type': 'text',
                'file_path': file_path,
                'error': str(e),
                'message': f'文本文件处理失败: {str(e)}'
            }
    
    def _process_excel_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None,
                            key_columns: str = None) -> Dict[str, Any]:
        """处理Excel文件差异"""
        import time
        start_time = time.time()
        
        try:
            # 延迟导入pandas以避免版本冲突
            try:
                import warnings
                # 抑制openpyxl的Data Validation和Conditional Formatting警告
                warnings.filterwarnings('ignore', message='Data Validation extension is not supported and will be removed')
                warnings.filterwarnings('ignore', message='Conditional Formatting extension is not supported and will be removed')
                
                import pandas as pd
                import openpyxl
            except ImportError as e:
                return {
                    'type': 'excel',
                    'file_path': file_path,
                    'error': f'缺少必要的Excel处理库: {str(e)}',
                    'message': '请安装pandas和openpyxl库来处理Excel文件'
                }
            
            # 处理Excel文件
            current_data = self._read_excel_data(current_content, file_path)
            previous_data = self._read_excel_data(previous_content, file_path) if previous_content else {}
            
            # 生成Excel差异
            diff_result = self._compare_excel_data(current_data, previous_data, file_path,
                                                   key_columns=key_columns)
            
            self.performance_stats['excel_diff_time'] += time.time() - start_time
            
            return diff_result
            
        except Exception as e:
            return {
                'type': 'excel',
                'file_path': file_path,
                'error': str(e),
                'message': f'Excel文件处理失败: {str(e)}'
            }
    
    def _process_image_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None) -> Dict[str, Any]:
        """处理图片文件差异"""
        import time
        start_time = time.time()
        
        try:
            # 将图片内容转换为base64编码
            current_base64 = base64.b64encode(current_content).decode('utf-8')
            previous_base64 = base64.b64encode(previous_content).decode('utf-8') if previous_content else None
            
            # 获取图片信息
            current_info = self._get_image_info(current_content)
            previous_info = self._get_image_info(previous_content) if previous_content else None
            
            # 检查图片是否相同
            is_same = current_content == previous_content if previous_content else False
            
            self.performance_stats['image_diff_time'] += time.time() - start_time
            
            return {
                'type': 'image',
                'file_path': file_path,
                'current_image': {
                    'base64': current_base64,
                    'info': current_info
                },
                'previous_image': {
                    'base64': previous_base64,
                    'info': previous_info
                } if previous_content else None,
                'is_same': is_same,
                'operation': 'added' if not previous_content else ('unchanged' if is_same else 'modified')
            }
            
        except Exception as e:
            return {
                'type': 'image',
                'file_path': file_path,
                'error': str(e),
                'message': f'图片文件处理失败: {str(e)}'
            }
    
    def _process_binary_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None) -> Dict[str, Any]:
        """处理二进制文件差异"""
        import time
        start_time = time.time()
        
        try:
            # 获取文件信息
            current_size = len(current_content)
            previous_size = len(previous_content) if previous_content else 0
            
            # 检查文件是否相同
            is_same = current_content == previous_content if previous_content else False
            
            # 获取MIME类型
            mime_type, _ = mimetypes.guess_type(file_path)
            
            self.performance_stats['binary_diff_time'] += time.time() - start_time
            
            return {
                'type': 'binary',
                'file_path': file_path,
                'current_size': current_size,
                'previous_size': previous_size,
                'size_change': current_size - previous_size,
                'is_same': is_same,
                'mime_type': mime_type,
                'operation': 'added' if not previous_content else ('unchanged' if is_same else 'modified'),
                'message': '二进制文件无法显示差异内容'
            }
            
        except Exception as e:
            return {
                'type': 'binary',
                'file_path': file_path,
                'error': str(e),
                'message': f'二进制文件处理失败: {str(e)}'
            }
    
    def _decode_text(self, content: bytes) -> str:
        """尝试解码文本内容"""
        if not content:
            return ""
        
        # 尝试多种编码
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1', 'cp1252']
        
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        # 如果所有编码都失败，使用utf-8并忽略错误
        return content.decode('utf-8', errors='replace')
    
    def _parse_unified_diff_lines(self, diff_lines: list) -> list:
        """解析unified diff格式为结构化数据"""
        hunks = []
        current_hunk = None
        
        for line in diff_lines:
            if line.startswith('@@'):
                # 新的hunk开始
                if current_hunk:
                    hunks.append(current_hunk)
                
                # 解析hunk头部信息
                match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if match:
                    old_start, old_count, new_start, new_count = match.groups()
                    current_hunk = {
                        'header': line.strip(),
                        'old_start': int(old_start),
                        'old_count': int(old_count) if old_count else 1,
                        'new_start': int(new_start),
                        'new_count': int(new_count) if new_count else 1,
                        'lines': []
                    }
            elif current_hunk and (line.startswith(' ') or line.startswith('+') or line.startswith('-')):
                # 添加diff行
                line_type = 'context' if line.startswith(' ') else ('added' if line.startswith('+') else 'removed')
                current_hunk['lines'].append({
                    'type': line_type,
                    'content': line[1:],  # 去掉前缀符号
                    'raw': line
                })
        
        if current_hunk:
            hunks.append(current_hunk)
        
        return hunks
    
    def _calculate_text_stats(self, hunks: list) -> Dict[str, int]:
        """计算文本差异统计信息"""
        stats = {'added': 0, 'removed': 0, 'modified': 0}
        
        for hunk in hunks:
            for line in hunk['lines']:
                if line['type'] == 'added':
                    stats['added'] += 1
                elif line['type'] == 'removed':
                    stats['removed'] += 1
        
        # 计算修改行数（成对的删除和添加）
        stats['modified'] = min(stats['added'], stats['removed'])
        stats['added'] -= stats['modified']
        stats['removed'] -= stats['modified']
        
        return stats
    
    def _read_excel_data(self, content: bytes, file_path: str) -> Dict[str, Any]:
        """读取Excel文件数据"""
        import pandas as pd
        import io
        import warnings
        
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
                return sheets
                
        except Exception as e:
            raise Exception(f"读取Excel文件失败: {str(e)}")
    
    def _compare_excel_data(self, current_data: Dict, previous_data: Dict, file_path: str,
                            key_columns: str = None) -> Dict[str, Any]:
        """比较Excel数据"""
        import pandas as pd
        
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
                                                  key_columns=key_columns)
            result['sheets'][sheet_name] = sheet_diff
            
            # 更新统计信息
            if 'stats' in sheet_diff:
                for key in ['added', 'removed', 'modified']:
                    result['summary'][key] += sheet_diff['stats'].get(key, 0)
        
        result['summary']['total'] = sum(result['summary'].values())
        
        return result
    
    def _compare_dataframes(self, current_df, previous_df, sheet_name: str,
                            key_columns: str = None) -> Dict[str, Any]:
        """比较两个DataFrame"""
        import pandas as pd
        
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
            headers = list(previous_df.columns) if previous_df is not None else []
            rows = []
            if previous_df is not None:
                rows = [
                    {
                        'row_number': row_number,
                        'status': 'removed',
                        'data': row_data,
                    }
                    for row_number, row_data in self._dataframe_rows_with_index(previous_df)
                ]
            return {
                'operation': 'deleted',
                'message': f'工作表 "{sheet_name}" 已被删除',
                'headers': headers,
                'rows': rows,
                'stats': {'added': 0, 'removed': len(previous_df) if previous_df is not None else 0, 'modified': 0}
            }
        
        if previous_df is None:
            # 新增工作表
            headers = list(current_df.columns)
            rows = [
                {
                    'row_number': row_number,
                    'status': 'added',
                    'data': row_data
                }
                for row_number, row_data in self._dataframe_rows_with_index(current_df)
            ]
            
            return {
                'operation': 'added',
                'message': f'新增工作表 "{sheet_name}"',
                'headers': headers,
                'rows': rows,
                'stats': {'added': len(current_df), 'removed': 0, 'modified': 0}
            }
        
        # 比较现有工作表
        return self._detailed_dataframe_comparison(current_df, previous_df, key_columns=key_columns)
    
    def _detailed_dataframe_comparison(self, current_df, previous_df, key_columns: str = None) -> Dict[str, Any]:
        """详细比较两个DataFrame，支持行插入/删除的智能识别"""
        import pandas as pd
        import numpy as np
        
        # 保持原始列顺序，优先使用当前文件的列顺序
        if current_df is not None:
            ordered_columns = list(current_df.columns)
            # 添加只在previous_df中存在的列
            if previous_df is not None:
                for col in previous_df.columns:
                    if col not in ordered_columns:
                        ordered_columns.append(col)
        elif previous_df is not None:
            ordered_columns = list(previous_df.columns)
        else:
            ordered_columns = []
        
        # 重新索引DataFrame以便比较，保持原始列顺序
        if current_df is not None:
            current_df = current_df.reindex(columns=ordered_columns, fill_value='')
        if previous_df is not None:
            previous_df = previous_df.reindex(columns=ordered_columns, fill_value='')
        
        # 使用智能diff算法处理行插入/删除
        return self._smart_row_diff(current_df, previous_df, ordered_columns, key_columns=key_columns)

    def _dataframe_rows_with_index(self, df):
        """高效转换 DataFrame 为带原始行号的记录列表。"""
        records = df.to_dict(orient='records')
        return [(idx + 1, row_data) for idx, row_data in enumerate(records)]
    
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

    def _smart_row_diff(self, current_df, previous_df, all_columns, key_columns=None) -> Dict[str, Any]:
        """智能行差异算法，正确处理行插入、删除和修改

        key_columns 有值时先按关键列配对（见 `_resolve_key_columns`），
        配上的行不再参与相似度匹配 —— 否则一个「ID 从 5 改成 7」的行会被
        相似度匹配认成「同一行被修改」，而按关键列的契约那是「删一行 + 加一行」。
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
            return {
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
            }
        
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
            
            if similarity < 1.0:
                # 计算具体的字段变更
                cell_changes = []
                for col in all_columns:
                    old_val = previous_row.get(col, '')
                    new_val = current_row.get(col, '')
                    
                    if not self._values_equal(old_val, new_val):
                        cell_changes.append({
                            'column': col,
                            'old_value': old_val,
                            'new_value': new_val
                        })
                
                rows.append({
                    'row_number': orig_row_num,
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
        
        return {
            'rows': rows,
            'stats': stats,
            'headers': all_columns,
            'columns': all_columns
        }
    
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
    
    def _find_row_matches(self, current_rows, previous_rows, columns,
                          index_offset_current=None, index_offset_previous=None):
        """优化的行匹配算法 - 减少时间复杂度

        index_offset_*：传入的是**子集**时（关键列阶段已经配掉的行不再参与相似度
        匹配），用它在返回前把下标映射回完整行列表的下标 —— 否则结果会指到错误的行上。
        """
        offsets_current = list(index_offset_current) if index_offset_current is not None             else list(range(len(current_rows)))
        offsets_previous = list(index_offset_previous) if index_offset_previous is not None             else list(range(len(previous_rows)))

        # 如果数据量很大，使用快速匹配策略（只是加速，阈值与下面同一套）
        if (len(current_rows) > self.LARGE_TABLE_ROW_THRESHOLD
                or len(previous_rows) > self.LARGE_TABLE_ROW_THRESHOLD):
            matches = self._fast_row_matching(current_rows, previous_rows, columns)
            return self._remap_match_indices(matches, offsets_current, offsets_previous)
        
        # 对于小数据集，使用精确匹配
        matches = []
        used_previous = set()
        
        for i, current_row in enumerate(current_rows):
            best_match = None
            best_score = 0
            
            # 早期退出：如果找到完全匹配，直接使用
            for j, previous_row in enumerate(previous_rows):
                if j in used_previous:
                    continue
                
                # 快速预检：比较关键字段
                if not self._quick_similarity_check(current_row, previous_row, columns):
                    continue
                
                # 计算详细相似度
                score = self._calculate_row_similarity(current_row, previous_row, columns)
                
                # 相似度阈值：与小表/大表统一（见 ROW_SIMILARITY_THRESHOLD 的说明）
                if score > self.ROW_SIMILARITY_THRESHOLD:
                    if score == 1.0:  # 完全匹配，直接使用
                        best_match = j
                        best_score = score
                        break
                    elif score > best_score:
                        best_match = j
                        best_score = score
            
            if best_match is not None:
                matches.append({
                    'type': 'match',
                    'current_idx': i,
                    'previous_idx': best_match,
                    'similarity': best_score
                })
                used_previous.add(best_match)
        
        matches.sort(key=lambda x: x['current_idx'])
        return self._remap_match_indices(matches, offsets_current, offsets_previous)

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
        remapped.sort(key=lambda x: x['current_idx'])
        return remapped

    def _fast_row_matching(self, current_rows, previous_rows, columns):
        """大数据集的快速匹配算法

        哈希索引的职责**只有一件事**：认出完全没变的行（相似度 == 1.0），
        把它们从后续的相似度扫描里摘出去，省下大部分比较。

        历史行为（已修）：这里曾用哈希（「非空值小写去空白」的拼串，本来就有碰撞）
        去接受 0.85 相似度的行 —— 等于让哈希碰撞决定配对结果，而小表路径的阈值是 0.6。
        同一个改动，表一大结论就变。现在两条路径共用 `ROW_SIMILARITY_THRESHOLD`。
        """
        matches = []
        
        # 创建哈希索引以加速查找
        previous_hashes = {}
        for j, row in enumerate(previous_rows):
            row_hash = self._calculate_row_hash(row, columns)
            if row_hash not in previous_hashes:
                previous_hashes[row_hash] = []
            previous_hashes[row_hash].append(j)
        
        used_previous = set()
        
        for i, current_row in enumerate(current_rows):
            current_hash = self._calculate_row_hash(current_row, columns)
            
            # 查找相同哈希的行：只接受**完全相等**的配对（哈希只是预筛）
            if current_hash in previous_hashes:
                best_j = None
                for j in previous_hashes[current_hash]:
                    if j not in used_previous:
                        if self._calculate_row_similarity(current_row, previous_rows[j], columns) == 1.0:
                            best_j = j
                            break
                
                if best_j is not None:
                    matches.append({
                        'type': 'match',
                        'current_idx': i,
                        'previous_idx': best_j,
                        'similarity': 1.0
                    })
                    used_previous.add(best_j)
        
        # 添加基于位置的匹配逻辑，用于处理部分修改的行
        used_current = set(match['current_idx'] for match in matches)
        position_matches = self._find_position_based_matches(current_rows, previous_rows, columns, used_previous, used_current)
        matches.extend(position_matches)
        
        matches.sort(key=lambda x: x['current_idx'])
        return matches
    
    def _find_position_based_matches(self, current_rows, previous_rows, columns, used_previous, used_current):
        """基于位置的匹配逻辑，用于识别部分修改的行

        搜索半径与相似度阈值都取自类常量，与其它路径**共用同一套**：
        搜索半径 = max(ROW_POSITION_SEARCH_MIN, 表大小 × ROW_POSITION_SEARCH_RATIO)，
        阈值 = ROW_SIMILARITY_THRESHOLD。
        """
        matches = []
        
        # 自适应搜索范围：至少 ROW_POSITION_SEARCH_MIN 行，最多为数据集大小的 ROW_POSITION_SEARCH_RATIO
        data_size = max(len(current_rows), len(previous_rows))
        search_range = max(self.ROW_POSITION_SEARCH_MIN,
                           int(data_size * self.ROW_POSITION_SEARCH_RATIO))
        
        # 对于未匹配的当前行，尝试与相近位置的前一版本行匹配
        for i, current_row in enumerate(current_rows):
            if i in used_current:
                continue
                
            # 搜索中心：优先以当前行号为中心
            center = i
            start_idx = max(0, center - search_range)
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
                
                # 与其它路径同一个阈值
                if score > self.ROW_SIMILARITY_THRESHOLD and score > best_score:
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
        
        return matches
    
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
    
    def calculate_excel_diff(self, current_content: bytes, previous_content: bytes, file_path: str,
                             key_columns: str = None) -> Dict[str, Any]:
        """计算Excel文件差异的公共接口"""
        return self._process_excel_diff(file_path, current_content, previous_content,
                                        key_columns=key_columns)
    
    def _get_image_info(self, content: bytes) -> Dict[str, Any]:
        """获取图片基本信息"""
        try:
            # 尝试使用PIL获取图片信息
            from PIL import Image
            import io
            
            img = Image.open(io.BytesIO(content))
            return {
                'width': img.width,
                'height': img.height,
                'format': img.format,
                'mode': img.mode,
                'size_bytes': len(content)
            }
        except ImportError:
            # 如果没有PIL，返回基本信息
            return {
                'size_bytes': len(content),
                'format': 'Unknown'
            }
        except Exception as e:
            return {
                'size_bytes': len(content),
                'format': 'Unknown',
                'error': str(e)
            }
