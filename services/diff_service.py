import base64
import codecs
import difflib
import mimetypes
import os
import re
from typing import Any, Dict, Optional

import pandas as pd

from utils.text_decoding import decode_text_bytes

from services.diff_excel_compare import (
    DiffExcelCompareMixin,
)
from services.diff_excel_reader import (
    DiffExcelReaderMixin,
    physical_row_number,  # noqa: F401 —— 测试与 git_service 按旧路径取
)
from services.diff_row_alignment import (
    DiffRowAlignmentMixin,
)



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


class DiffService(
    DiffExcelCompareMixin,
    DiffExcelReaderMixin,
    DiffRowAlignmentMixin,
):
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
                     header_name_row: int = None, marker_column: str = None) -> Dict[str, Any]:
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
                                                header_name_row=header_name_row,
                                                marker_column=marker_column)
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
                             key_columns: str = None,
                             header_rows: int = None,
                             header_name_row: int = None,
                             marker_column: str = None) -> Dict[str, Any]:
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
        return self._compare_excel_data({}, previous_data, file_path, key_columns=key_columns,
                                        header_rows=header_rows,
                                        header_name_row=header_name_row,
                                        marker_column=marker_column)

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
                            header_name_row: int = None, marker_column: str = None) -> Dict[str, Any]:
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
                                                   header_name_row=header_name_row,
                                                   marker_column=marker_column)
            
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
    

    # 布尔单元格在 dtype=str 下的两种写法（见 _repair_inferred_cells）
    _BOOL_TEXTS = ('True', 'False')

    
    







    












    

    







    
    
    

    
    
    
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
