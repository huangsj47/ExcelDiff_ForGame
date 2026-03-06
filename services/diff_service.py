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
    
    def process_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None) -> Dict[str, Any]:
        """处理文件差异，根据文件类型选择合适的处理方式"""
        file_type = self.get_file_type(file_path)
        
        try:
            if file_type == 'text':
                return self._process_text_diff(file_path, current_content, previous_content)
            elif file_type == 'excel':
                return self._process_excel_diff(file_path, current_content, previous_content)
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
    
    def _process_excel_diff(self, file_path: str, current_content: bytes, previous_content: bytes = None) -> Dict[str, Any]:
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
            diff_result = self._compare_excel_data(current_data, previous_data, file_path)
            
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
            
            if ext == '.csv':
                # CSV文件处理
                text_content = self._decode_text(content)
                df = pd.read_csv(io.StringIO(text_content))
                return {'Sheet1': df}
            else:
                # Excel文件处理
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message='Data Validation extension is not supported and will be removed')
                    warnings.filterwarnings('ignore', message='Conditional Formatting extension is not supported and will be removed')
                    excel_file = pd.ExcelFile(io.BytesIO(content))
                    sheets = {}
                    for sheet_name in excel_file.sheet_names:
                        sheets[sheet_name] = pd.read_excel(excel_file, sheet_name=sheet_name)
                return sheets
                
        except Exception as e:
            raise Exception(f"读取Excel文件失败: {str(e)}")
    
    def _compare_excel_data(self, current_data: Dict, previous_data: Dict, file_path: str) -> Dict[str, Any]:
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
            
            sheet_diff = self._compare_dataframes(current_df, previous_df, sheet_name)
            result['sheets'][sheet_name] = sheet_diff
            
            # 更新统计信息
            if 'stats' in sheet_diff:
                for key in ['added', 'removed', 'modified']:
                    result['summary'][key] += sheet_diff['stats'].get(key, 0)
        
        result['summary']['total'] = sum(result['summary'].values())
        
        return result
    
    def _compare_dataframes(self, current_df, previous_df, sheet_name: str) -> Dict[str, Any]:
        """比较两个DataFrame"""
        import pandas as pd
        
        if current_df is None and previous_df is None:
            return {'headers': [], 'rows': [], 'stats': {'added': 0, 'removed': 0, 'modified': 0}}
        
        if current_df is None:
            # 工作表被删除
            return {
                'operation': 'deleted',
                'message': f'工作表 "{sheet_name}" 已被删除',
                'headers': list(previous_df.columns) if previous_df is not None else [],
                'rows': [],
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
        return self._detailed_dataframe_comparison(current_df, previous_df)
    
    def _detailed_dataframe_comparison(self, current_df, previous_df) -> Dict[str, Any]:
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
        return self._smart_row_diff(current_df, previous_df, ordered_columns)

    def _dataframe_rows_with_index(self, df):
        """高效转换 DataFrame 为带原始行号的记录列表。"""
        records = df.to_dict(orient='records')
        return [(idx + 1, row_data) for idx, row_data in enumerate(records)]
    
    def _smart_row_diff(self, current_df, previous_df, all_columns) -> Dict[str, Any]:
        """智能行差异算法，正确处理行插入、删除和修改"""
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
        
        # 使用改进的匹配算法
        matches = self._find_row_matches(current_rows, previous_rows, all_columns)
        
        # 创建匹配映射
        current_matched = set()
        previous_matched = set()
        
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
        """检查行是否包含有效数据"""
        import pandas as pd
        
        for col in columns:
            val = row_data.get(col, '')
            if val is not None and not pd.isna(val):
                val_str = str(val).strip().lower()
                if val_str not in ['', 'nan', 'none', 'null', '<na>']:
                    return True
        return False
    
    def _filter_nan_rows(self, rows, columns):
        """过滤掉全NaN的空行"""
        import pandas as pd
        import numpy as np
        
        filtered_rows = []
        for row in rows:
            # 检查是否所有值都是NaN或空
            has_data = False
            for col in columns:
                val = row.get(col, '')
                # 改进NaN值的判断逻辑
                if val is not None and not pd.isna(val):
                    val_str = str(val).strip().lower()
                    if val_str not in ['', 'nan', 'none', 'null', '<na>']:
                        has_data = True
                        break
            
            if has_data:
                filtered_rows.append(row)
        
        return filtered_rows
    
    def _find_row_matches(self, current_rows, previous_rows, columns):
        """优化的行匹配算法 - 减少时间复杂度"""
        matches = []
        
        # 如果数据量很大，使用快速匹配策略
        if len(current_rows) > 100 or len(previous_rows) > 100:
            return self._fast_row_matching(current_rows, previous_rows, columns)
        
        # 对于小数据集，使用精确匹配
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
                
                # 降低相似度阈值以更好地识别修改行
                if score > 0.6:  # 从0.95降低到0.6
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
        return matches
    
    def _fast_row_matching(self, current_rows, previous_rows, columns):
        """大数据集的快速匹配算法（改进版）
        
        改进点：
        1. 哈希匹配阈值从0.95降至0.85，允许命中轻微修改的行
        2. 哈希未命中的行也进入位置匹配阶段，避免遗漏
        3. 位置匹配搜索范围自适应数据集大小
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
            
            # 查找相同哈希的行
            if current_hash in previous_hashes:
                best_j = None
                best_score = 0
                for j in previous_hashes[current_hash]:
                    if j not in used_previous:
                        # 验证是否真正匹配
                        score = self._calculate_row_similarity(current_row, previous_rows[j], columns)
                        if score == 1.0:
                            best_j = j
                            best_score = score
                            break
                        elif score > 0.85 and score > best_score:
                            best_j = j
                            best_score = score
                
                if best_j is not None:
                    matches.append({
                        'type': 'match',
                        'current_idx': i,
                        'previous_idx': best_j,
                        'similarity': best_score
                    })
                    used_previous.add(best_j)
        
        # 添加基于位置的匹配逻辑，用于处理部分修改的行
        used_current = set(match['current_idx'] for match in matches)
        position_matches = self._find_position_based_matches(current_rows, previous_rows, columns, used_previous, used_current)
        matches.extend(position_matches)
        
        matches.sort(key=lambda x: x['current_idx'])
        return matches
    
    def _find_position_based_matches(self, current_rows, previous_rows, columns, used_previous, used_current):
        """基于位置的匹配逻辑，用于识别部分修改的行（改进版）
        
        改进点：
        1. 搜索范围从固定±3改为自适应：max(10, 数据集大小的10%)
        2. 使用累计偏移量跟踪插入/删除导致的整体位移
        3. 相似度阈值降至0.5以覆盖更多修改场景
        """
        matches = []
        
        # 自适应搜索范围：至少10行，最多为数据集大小的10%
        data_size = max(len(current_rows), len(previous_rows))
        search_range = max(10, int(data_size * 0.1))
        
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
                
                # 使用更宽松的阈值（0.5）覆盖较大修改
                if score > 0.5 and score > best_score:
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
        
        改进点：阈值从 >0 修正为 >=2（与注释语义对齐），
        同时对仅有1-2个关键列的情况做降级处理
        """
        key_columns = columns[:min(3, len(columns))]
        
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
        """标准化单元格值，统一处理NaN/空值（#31: 提取为类方法，消除热路径闭包开销）"""
        if val is None:
            return None
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        val_str = str(val).strip().lower()
        if val_str in ('', 'nan', 'none', 'null', '<na>'):
            return None
        return str(val).strip()

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
    
    def calculate_excel_diff(self, current_content: bytes, previous_content: bytes, file_path: str) -> Dict[str, Any]:
        """计算Excel文件差异的公共接口"""
        return self._process_excel_diff(file_path, current_content, previous_content)
    
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
