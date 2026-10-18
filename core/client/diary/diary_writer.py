# coding: utf-8
"""
日记写入模块

提供 DiaryWriter 类用于将识别结果写入 Markdown 日记文件。
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import date, datetime, timedelta
from os import makedirs
from pathlib import Path
from typing import Optional, List

from core.client.state import console
from . import logger


# MD 文件头部模板
HEADER_MD = r'''```txt
正则表达式 Tip

匹配到音频文件链接：\[(.+)\]\((.{10,})\)[\s]*
替换为 HTML 控件：<audio controls><source src="$2" type="audio/mpeg">$1</audio>\n\n

匹配 HTML 控件：<audio controls><source src="(.+)" type="audio/mpeg">(.+)</audio>\n\n
替换为文件链接：[$2]($1) 
```


'''


class DiaryWriter:
    """
    日记写入器
    
    负责将识别结果写入 Markdown 日记文件：
    - 根据日期创建文件夹结构
    - 链接音频文件
    """
    
    def __init__(self, base_path: Optional[Path] = None):
        """
        初始化日记写入器
        
        Args:
            base_path: 基础路径，默认为当前目录
        """
        self.base_path = base_path or Path()

    # ---------- 过期数据清理 ----------

    _AUDIO_NAME_RE = re.compile(r'(\d{8})-\d{6}')
    _AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

    def prune(self, keep_days: int = 1) -> int:
        """
        清理过期的日记与音频文件

        在每次客户端启动时调用，保证磁盘上只留最近若干天的内容。
        keep_days=1 表示只保留今天。

        Args:
            keep_days: 保留天数，1 表示只保留今天

        Returns:
            删除的文件数量
        """
        keep_days = max(1, int(keep_days))
        cutoff = date.today() - timedelta(days=keep_days - 1)
        removed = 0

        if not self.base_path.is_dir():
            return 0

        for year_dir in self.base_path.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue

                removed += self._prune_month(month_dir, cutoff)

                # 空目录一并清掉
                if not any(month_dir.iterdir()):
                    month_dir.rmdir()
                if not any(year_dir.iterdir()):
                    year_dir.rmdir()

        if removed:
            logger.info(f'已清理 {removed} 个过期文件（保留 {keep_days} 天）')
        return removed

    def _prune_month(self, month_dir: Path, cutoff: date) -> int:
        """清理单个月份目录下的过期文件"""
        removed = 0

        for md_file in month_dir.glob('*.md'):
            try:
                day = int(md_file.stem)
                file_date = date(int(month_dir.parent.name), int(month_dir.name), day)
            except (ValueError, TypeError):
                continue  # 名字不是日期，保留
            if file_date < cutoff:
                md_file.unlink(missing_ok=True)
                removed += 1

        assets = month_dir / 'assets'
        if assets.is_dir():
            for audio in assets.iterdir():
                if audio.suffix.lower() not in self._AUDIO_EXTS:
                    continue
                m = self._AUDIO_NAME_RE.search(audio.name)
                if not m:
                    continue
                try:
                    file_date = datetime.strptime(m.group(1), '%Y%m%d').date()
                except ValueError:
                    continue
                if file_date < cutoff:
                    audio.unlink(missing_ok=True)
                    removed += 1
            if not any(assets.iterdir()):
                assets.rmdir()

        return removed
    
    def write(
        self,
        text: str,
        time_start: float,
        file_audio: Optional[Path] = None
    ) -> List[Path]:
        """
        写入日记
        
        Args:
            text: 识别文本
            time_start: 录音开始时间戳
            file_audio: 音频文件路径（可选）
            
        Returns:
            写入的日记文件路径列表
        """
        local_time = time.localtime(time_start)
        time_year = time.strftime('%Y', local_time)
        time_month = time.strftime('%m', local_time)
        time_day = time.strftime('%d', local_time)
        time_hms = time.strftime('%H:%M:%S', local_time)
        
        folder_path = self.base_path / time_year / time_month
        makedirs(folder_path, exist_ok=True)
        
        # 按日期归档
        file_md = folder_path / f'{time_day}.md'
        
        # 确保 md 文件存在
        if not file_md.exists():
            self._create_md(file_md)
        
        # 构建日记条目
        if file_audio:
            try:
                path_rel = file_audio.relative_to(file_md.parent).as_posix()
                path_rel = path_rel.replace(" ", "%20")
            except ValueError:
                path_rel = file_audio.as_posix().replace(" ", "%20")
        else:
            path_rel = ""
        
        # 写入 md
        with open(file_md, 'a', encoding='utf-8') as f:
            if path_rel:
                f.write(f'[{time_hms}]({path_rel}) {text}\n\n')
            else:
                f.write(f'{time_hms} {text}\n\n')
        
        logger.debug(f"写入日记: {file_md.name}")
        return file_md
    
    def _create_md(self, file_md: Path) -> None:
        """创建新的 MD 文件"""
        with open(file_md, 'w', encoding='utf-8') as f:
            f.write(HEADER_MD)
        logger.debug(f"创建日记文件: {file_md}")
