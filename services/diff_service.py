import base64
import codecs
import difflib
import mimetypes
import os
import re
from typing import Any, Dict, Optional

import pandas as pd

from utils.text_decoding import decode_text_bytes

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


# ---------------------------------------------------------------------------
#  「扩展名不认识时，看内容判类型」——只在这一处实现
# ---------------------------------------------------------------------------
# 判据：**没有 NUL 字节，且能按 UTF-8 或 GBK 严格解码**。
#   * NUL 是二进制最可靠的信号（`.png`/`.zip`/`.woff2`/`.bin` 全都命中），
#     用它而不是「能不能解码」：`latin-1` 能解码任意字节，拿它当判据等于没有判据。
#   * 中文项目里 GBK 的源码/配置很常见（与 `_decode_text` 的编码清单同源）。
#   * 只嗅前若干字节：判断「是不是文本」不需要读完整个文件，而大文件读全文只是浪费。
_TEXT_SNIFF_BYTES = 8192
_TEXT_SNIFF_ENCODINGS = ('utf-8', 'gbk')


def looks_like_text(content: Optional[bytes]) -> bool:
    """扩展名认不出来时，靠内容判断这是不是一个文本文件。

    **尾部被截断的多字节字符不算失败**：样本正好切在一个汉字中间时，严格解码会抛
    `UnicodeDecodeError`，于是「明明是文本却判成二进制」。所以用增量解码器
    （`final=False`）—— 它容忍结尾不完整，只对真正非法的字节报错。
    """
    if not content:
        return False
    sample = bytes(content[:_TEXT_SNIFF_BYTES])
    if b'\x00' in sample:
        return False
    for encoding in _TEXT_SNIFF_ENCODINGS:
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            decoder.decode(sample, False)
        except UnicodeDecodeError:
            continue
        return True
    return False


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
    #  `len(rows) > 100` 走大表路径（哈希阈值 0.85、位置匹配 0.5），
    #  否则走小表路径（阈值 0.6）。于是**同一个改动、同一份表，仅仅因为行数跨过
    #  100 行，结论就不同**：既不可复现，也让「为什么这张表准、那张表不准」无法解释
    #  （线上审计：5988 报 3 行而 git 是 456 行的表，正是大表路径）。
    #  大表分支与它专用的那一套阈值已经删掉，只剩下面这一条路径。
    #
    #  哈希索引保留，但它的职责收窄成「认出完全没变的行」——哈希是「非空值小写去空白」
    #  的拼串，本来就有碰撞，用它去接受 0.85 相似度的行等于让碰撞决定配对结果。
    #  变了多少一律交给对齐阶段，用同一个阈值判定。
    # ------------------------------------------------------------------
    ROW_SIMILARITY_THRESHOLD = 0.6      # 认定为「同一行被修改」的最低相似度
    ROW_POSITION_SEARCH_MIN = 10        # 位置匹配的最小搜索半径
    ROW_POSITION_SEARCH_RATIO = 0.1     # 搜索半径 = 表大小 × 该比例（取两者较大值）
    DEFAULT_KEY_COLUMN_COUNT = 3        # 未配置关键列时，快速预检用前 N 列
    ALIGN_SEGMENT_MAX_CELLS = 200_000   # 段内 DP 的规模上限（行数乘积），超过退回贪心

    def __init__(self):
        self.performance_stats = {
            'text_diff_time': 0,
            'excel_diff_time': 0,
            'image_diff_time': 0,
            'binary_diff_time': 0
        }
    
    def get_file_type(self, file_path: str, content: bytes = None) -> str:
        """判断文件类型：**先看扩展名，扩展名不认识时再看内容**。

        ## 为什么要有第二眼（2026-09-19）

        只有扩展名清单时，任何不在清单里的纯文本文件都会被判成 `binary`，而
        `_process_binary_diff` 给出的载荷是「二进制文件无法显示差异内容」——
        于是**协议定义文件（`.proto`）在页面上与 AI 眼里都是「看不见的二进制」**：
        报告里那句「未读到任何协议 diff」就是这么来的（AI 读的是同一份载荷）。

        判据用**内容**而不是继续往清单里加扩展名：清单是有限枚举，下一次出现新的
        文本类型（`.toml`、`.ts`……）会再犯一次；而「是不是文本」看字节就知道。

        `content` 是可选的：拿得到字节的调用点（`process_diff`）传进来，只拿得到路径的
        调用点（页面元数据）行为一个字不变 —— 认不出来的扩展名照旧 `binary`，
        免得一个恰好能解码的 `.bin` 被当作文本。（既有断言：
        `test_business_chain_integration.py` 钉着 `.bin`/`.zip` 是 binary。）
        """
        ext = os.path.splitext(file_path.lower())[1]

        if ext in self.EXCEL_EXTENSIONS:
            return 'excel'
        elif ext in self.CSV_EXTENSIONS:
            return 'excel'  # CSV也作为Excel处理
        elif ext in self.TEXT_EXTENSIONS:
            return 'text'
        elif ext in self.IMAGE_EXTENSIONS:
            return 'image'
        elif looks_like_text(content):
            return 'text'
        else:
            return 'binary'
    
    def process_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None,
                     key_columns: str = None, header_rows: int = None,
                     header_name_row: int = None) -> Dict[str, Any]:
        """处理文件差异，根据文件类型选择合适的处理方式。

        key_columns：仓库上配置的「关键列」（`Repository.key_columns`，列号从 1 开始、
        英文逗号分隔，如 `1,2,3`）。配了它，行匹配就按这些列的值认「同一行」——
        这是帮助文档已经写明的契约（`templates/help.html` 的「关键列」一节），
        但引擎历史上从来没读过这个配置，只按前 3 列的相似度猜配对，
        于是把不同的行配成一条「修改」，报出来的改前值是另一行的
        （线上审计：5988 报的行 9 里 old 取自第 21 行、new 取自第 10 行）。

        header_rows：仓库上配置的「表头行数」（`Repository.header_rows`）。这个值
        **只用来把表头行与数据行分开**，不改读取方式：第 1 行照旧是列名（`header=0`），
        物理行 `2..header_rows` 归到每张表自己的 `header_rows` 块里，`rows`/`stats`
        只剩数据行。详见 `_build_header_rows`。

        header_name_row：仓库上配置的「名称行」（`Repository.header_name_row`）——
        **列名取表头块里的第几行**。1（或空）就是今天的行为（第 1 行是字段名行）；
        配 2 用于「第 1 行是大标题、第 2 行才是字段名」的表。取列名的动作放在
        改名之后仍按 `header=0` 的读法进行（见 `_plan_name_row`），所以行号口径
        （物理行 `idx + 2`）一个字都没变。
        """
        # 带上字节：扩展名不认识的文件（`.proto` 这类纯文本）要靠内容才能判对类型，
        # 见 `get_file_type` 与 `looks_like_text`。
        file_type = self.get_file_type(file_path, current_content)

        try:
            if file_type == 'text':
                return self._process_text_diff(file_path, current_content, previous_content)
            elif file_type == 'excel':
                return self._process_excel_diff(file_path, current_content, previous_content,
                                                key_columns=key_columns,
                                                header_rows=header_rows,
                                                header_name_row=header_name_row)
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
    
    def process_deleted_file(self, file_path: str, previous_content: bytes,
                             header_rows: int = None,
                             header_name_row: int = None) -> Dict[str, Any]:
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
        return self._compare_excel_data({}, previous_data, file_path, header_rows=header_rows,
                                        header_name_row=header_name_row)

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
                # 逐行剥掉行尾换行后再用 '\n' 连接。
                #
                # 这里两种想当然的写法都不对，原因在 unified_diff 的两种行混在一起：
                #   * 正文行来自 `splitlines(keepends=True)`，**自带换行符**；
                #   * `--- a/x` / `+++ b/x` / `@@ … @@` 这三行是 difflib 自己合成的，
                #     在 `lineterm=""` 下**不带换行符**。
                # 于是 `''.join` 会把三个头行和紧随其后的第一行正文粘成一整行
                # （`--- a/x.lua+++ b/x.lua@@ -1,3 +1,5 @@ function …`），
                # 而 `'\n'.join` 又给正文行多加一个空行、整份 diff 变成双倍行距。
                'raw_diff': '\n'.join(line.rstrip('\r\n') for line in diff_lines),
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
                            key_columns: str = None, header_rows: int = None,
                            header_name_row: int = None) -> Dict[str, Any]:
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
                                                   key_columns=key_columns,
                                                   header_rows=header_rows,
                                                   header_name_row=header_name_row)
            
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
        """尝试解码文本内容。

        编码清单**不在这里**：`utils/text_decoding.decode_text_bytes` 是全平台唯一一份
        （AI 取数层原先各写一份，于是同一串字节在「diff 引擎」与「AI 取到的正文」里
        是两种文本）。这里保留方法名是因为调用方与测试都在用它；语义一个字没改：
        依次试 utf-8 → gbk → gb2312 → latin-1 → cp1252，最后以 `errors='replace'` 兜底。
        """
        return decode_text_bytes(content)
    
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

    # 布尔单元格在 dtype=str 下的两种写法（见 _repair_inferred_cells）
    _BOOL_TEXTS = ('True', 'False')

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
                            header_name_row: int = None) -> Dict[str, Any]:
        """比较Excel数据"""
        import pandas as pd

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
                                                  header_name_row=name_row)
            result['sheets'][sheet_name] = sheet_diff
            
            # 更新统计信息
            if 'stats' in sheet_diff:
                for key in ['added', 'removed', 'modified']:
                    result['summary'][key] += sheet_diff['stats'].get(key, 0)
        
        result['summary']['total'] = sum(result['summary'].values())
        
        return result
    
    def _compare_dataframes(self, current_df, previous_df, sheet_name: str,
                            key_columns: str = None, header_rows: int = None,
                            header_name_row: int = None) -> Dict[str, Any]:
        """比较两个DataFrame

        header_rows 是「表头块占前几行」（含第 1 行的列名行，见 `_header_row_count`）。
        整个工作表增/删的两条分支同样要把表头行分出来 —— 否则新加一张三行表头的表，
        表头那两行会被算进「新增 N 行」的计数里。

        header_name_row 是名称行的物理行号（见 `_header_name_row`）。增/删工作表这两条
        分支也要按它取列名：不然同一张表在「整表新增」与「改了一格」两种提交里，
        列头会显示成两套名字（一边是第 1 行的标题占位名、一边是字段名）。
        """
        import pandas as pd

        header_count = self._header_row_count(header_rows)
        name_row = self._header_name_row(header_name_row, header_count)

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
            if DiffService._is_placeholder_column_name(name):
                continue
            index.setdefault(name, []).append(j)

        anchors = []
        used_previous = set()
        last_previous = -1
        for i, name in enumerate(current_columns):
            if DiffService._is_placeholder_column_name(name):
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
            import io
            from PIL import Image

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
