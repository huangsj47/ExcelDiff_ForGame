#!/usr/bin/env python3
"""
从HTML中提取diff修改行信息
"""
import re
import requests

def extract_diff_lines():
    """从diff页面提取修改行信息"""
    
    try:
        # 获取diff页面内容
        response = requests.get("http://localhost:8002/commits/23/full-diff")
        html_content = response.text
        
        # 查找所有的diff行
        diff_pattern = r'<div class="diff-line (added|removed)">\s*<div class="line-number">\s*(\d*)\s*</div>'
        matches = re.findall(diff_pattern, html_content)
        
        changes = []
        for match in matches:
            change_type, line_number = match
            if line_number.strip():  # 只处理有行号的行
                changes.append({
                    'line': int(line_number),
                    'type': change_type
                })
        
        print("提取到的修改行:")
        for i, change in enumerate(changes):
            print(f"{i+1}. 第{change['line']}行 - {change['type']}")
        
        print(f"\n总修改行数: {len(changes)}")
        
        # 应用合并算法
        def merge_consecutive_diffs(diffs):
            if len(diffs) == 0:
                return []
            
            # 按行号排序
            sorted_diffs = sorted(diffs, key=lambda x: x['line'])
            
            merged = []
            current_group = [sorted_diffs[0]]
            
            for i in range(1, len(sorted_diffs)):
                current = sorted_diffs[i]
                previous = sorted_diffs[i-1]
                
                # 如果当前行与前一行连续（行号相差3以内），则合并到同一组
                if current['line'] - previous['line'] <= 3:
                    current_group.append(current)
                else:
                    # 创建合并的修改项
                    merged_diff = {
                        'start_line': current_group[0]['line'],
                        'end_line': current_group[-1]['line'],
                        'count': len(current_group),
                        'changes': current_group
                    }
                    merged.append(merged_diff)
                    current_group = [current]
            
            # 处理最后一组
            if len(current_group) > 0:
                merged_diff = {
                    'start_line': current_group[0]['line'],
                    'end_line': current_group[-1]['line'],
                    'count': len(current_group),
                    'changes': current_group
                }
                merged.append(merged_diff)
            
            return merged
        
        merged_changes = merge_consecutive_diffs(changes)
        
        print("\n合并后的修改组:")
        for i, group in enumerate(merged_changes):
            if group['start_line'] == group['end_line']:
                print(f"{i+1}. 第{group['start_line']}行 (1行修改)")
            else:
                print(f"{i+1}. 第{group['start_line']}-{group['end_line']}行 ({group['count']}行修改)")
            
            for change in group['changes']:
                print(f"   - 第{change['line']}行: {change['type']}")
        
        print(f"\n合并后的修改处数: {len(merged_changes)}")
        
        # 分析间距
        print("\n修改组间距分析:")
        for i in range(len(merged_changes) - 1):
            current_end = merged_changes[i]['end_line']
            next_start = merged_changes[i+1]['start_line']
            gap = next_start - current_end
            print(f"第{i+1}组到第{i+2}组间距: {gap}行 ({'会合并' if gap <= 3 else '不会合并'})")
        
        return merged_changes
        
    except Exception as e:
        print(f"错误: {e}")
        return []

if __name__ == "__main__":
    extract_diff_lines()
