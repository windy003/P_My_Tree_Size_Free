# -*- coding: utf-8 -*-
"""
磁盘空间分析器 (Disk Usage Analyzer)

功能:
  - 选择一个驱动器或文件夹
  - 列出其中所有直接子条目(文件和文件夹)的大小,从大到小排列
  - 文件夹大小为其内部所有文件的递归总和
  - 双击文件夹可进入下一级,"上一级"按钮可返回
  - 后台线程扫描,不阻塞界面;可中途取消

依赖:仅标准库 (tkinter)。运行: python disk_usage.py
"""

import os
import sys
import string
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def human_size(num_bytes):
    """把字节数转换成易读的字符串,如 1.5 GB。"""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0


def list_drives():
    """返回当前系统中存在的驱动器列表,如 ['C:\\\\', 'D:\\\\']。"""
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


def folder_size(path, cancel_event):
    """递归计算文件夹内所有文件大小之和。遇到无权限/出错的项跳过。"""
    total = 0
    stack = [path]
    while stack:
        if cancel_event.is_set():
            return total
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if cancel_event.is_set():
                        return total
                    try:
                        if entry.is_symlink():
                            # 不跟随符号链接,避免重复统计或死循环
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return total


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class DiskUsageApp:
    def __init__(self, root):
        self.root = root
        self.root.title("磁盘空间分析器")
        self.root.geometry("900x600")  # 还原(非最大化)时的窗口尺寸
        # 打开时默认最大化
        try:
            self.root.state("zoomed")          # Windows
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)  # Linux 等
            except tk.TclError:
                pass

        self.current_path = None
        self.scan_generation = 0          # 用于丢弃过期的扫描结果
        self.cancel_event = threading.Event()
        self.item_data = {}               # iid -> dict(path, size, is_dir)
        self.sort_column = "size"
        self.sort_reverse = True          # 默认按大小从大到小
        self.base_size = 14               # 界面基础字号

        self._setup_fonts()
        self._build_ui()
        self._populate_drives()

    # ----- 字体/样式(放大字号) ----------------------------------------
    def _setup_fonts(self):
        import tkinter.font as tkfont

        base_size = self.base_size

        # 把 tk 内置的几个具名字体整体放大,影响 Label/Button/Combobox 等
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=base_size)
            except tk.TclError:
                pass

        style = ttk.Style()
        ui_font = ("Microsoft YaHei UI", base_size)
        row_height = int(base_size * 2.2)

        style.configure("Treeview", font=ui_font, rowheight=row_height)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", base_size, "bold"))
        style.configure("TButton", font=ui_font, padding=4)
        style.configure("TLabel", font=ui_font)
        style.configure("TCombobox", font=ui_font)
        self.root.option_add("*TCombobox*Listbox.font", ui_font)

    def _change_font(self, delta):
        self.base_size = max(8, min(40, self.base_size + delta))
        self._setup_fonts()
        if hasattr(self, "_results"):
            self._render(self._results)

    # ----- 界面构建 -------------------------------------------------------
    def _build_ui(self):
        # 顶部工具栏
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(toolbar, text="驱动器:").pack(side=tk.LEFT)
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(
            toolbar, textvariable=self.drive_var, width=8, state="readonly"
        )
        self.drive_combo.pack(side=tk.LEFT, padx=(2, 8))
        self.drive_combo.bind("<<ComboboxSelected>>", self._on_drive_selected)

        ttk.Button(toolbar, text="选择文件夹…", command=self._choose_folder).pack(
            side=tk.LEFT, padx=2
        )
        self.up_btn = ttk.Button(
            toolbar, text="上一级", command=self._go_up, state=tk.DISABLED
        )
        self.up_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="刷新", command=self._refresh).pack(
            side=tk.LEFT, padx=2
        )
        self.cancel_btn = ttk.Button(
            toolbar, text="停止", command=self._cancel_scan, state=tk.DISABLED
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=2)

        # 字号调节
        ttk.Button(toolbar, text="A+", width=3,
                   command=lambda: self._change_font(2)).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="A−", width=3,
                   command=lambda: self._change_font(-2)).pack(side=tk.RIGHT, padx=2)
        ttk.Label(toolbar, text="字号:").pack(side=tk.RIGHT)

        # 当前路径显示
        path_frame = ttk.Frame(self.root, padding=(8, 0))
        path_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(path_frame, text="路径:").pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value="(未选择)")
        ttk.Label(path_frame, textvariable=self.path_var, foreground="#0a5").pack(
            side=tk.LEFT, padx=4
        )

        # 列表 (Treeview)
        table_frame = ttk.Frame(self.root, padding=8)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("size", "type", "percent")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.tree.heading("#0", text="名称", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="大小", command=lambda: self._sort_by("size"))
        self.tree.heading("type", text="类型", command=lambda: self._sort_by("type"))
        self.tree.heading("percent", text="占比", command=lambda: self._sort_by("size"))

        self.tree.column("#0", width=420, anchor=tk.W)
        self.tree.column("size", width=120, anchor=tk.E)
        self.tree.column("type", width=100, anchor=tk.CENTER)
        self.tree.column("percent", width=160, anchor=tk.W)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._on_double_click)

        # 像资源管理器一样用键盘导航
        #   ↑/↓ 选择条目(Treeview 自带);Enter 进入目录;Alt+↑ 回上一级
        self.tree.bind("<Return>", self._on_enter)
        self.root.bind("<Alt-Up>", lambda e: (self._go_up(), "break")[1])

        # 键盘快捷键:Ctrl+加号放大,Ctrl+减号缩小
        self.root.bind("<Control-plus>", lambda e: self._change_font(2))
        self.root.bind("<Control-equal>", lambda e: self._change_font(2))
        self.root.bind("<Control-minus>", lambda e: self._change_font(-2))

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪。请选择驱动器或文件夹。")
        status = ttk.Label(
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W,
            padding=(6, 3)
        )
        status.pack(side=tk.BOTTOM, fill=tk.X)

    def _populate_drives(self):
        drives = list_drives()
        self.drive_combo["values"] = drives

    # ----- 事件处理 -------------------------------------------------------
    def _on_drive_selected(self, _event=None):
        drive = self.drive_var.get()
        if drive:
            self.scan(drive)

    def _choose_folder(self):
        folder = filedialog.askdirectory(title="选择一个文件夹")
        if folder:
            self.scan(os.path.normpath(folder))

    def _go_up(self):
        if not self.current_path:
            return
        parent = os.path.dirname(self.current_path.rstrip("\\/"))
        if parent and os.path.exists(parent) and parent != self.current_path:
            self.scan(parent)

    def _refresh(self):
        if self.current_path:
            self.scan(self.current_path)

    def _on_double_click(self, _event):
        iid = self.tree.focus()
        info = self.item_data.get(iid)
        if info and info["is_dir"]:
            self.scan(info["path"])

    def _on_enter(self, _event):
        """Enter:进入选中的目录(文件则忽略)。"""
        iid = self.tree.focus()
        info = self.item_data.get(iid)
        if info and info["is_dir"]:
            self.scan(info["path"])
        return "break"

    def _cancel_scan(self):
        self.cancel_event.set()
        self.status_var.set("正在停止…")

    # ----- 扫描逻辑 -------------------------------------------------------
    def scan(self, path):
        """开始扫描指定路径(在后台线程中)。"""
        if not os.path.isdir(path):
            messagebox.showerror("错误", f"路径不存在或不是文件夹:\n{path}")
            return

        # 取消上一次扫描
        self.cancel_event.set()
        self.scan_generation += 1
        generation = self.scan_generation
        self.cancel_event = threading.Event()

        self.current_path = path
        self.path_var.set(path)

        # 同步驱动器下拉框
        if os.name == "nt" and len(path) >= 2 and path[1] == ":":
            drive_root = path[:2].upper() + "\\"
            if drive_root in self.drive_combo["values"]:
                self.drive_var.set(drive_root)

        # 是否能"上一级"
        parent = os.path.dirname(path.rstrip("\\/"))
        self.up_btn.config(
            state=tk.NORMAL if parent and parent != path and os.path.exists(parent)
            else tk.DISABLED
        )

        self.tree.delete(*self.tree.get_children())
        self.item_data.clear()
        self.cancel_btn.config(state=tk.NORMAL)
        self.status_var.set("正在读取目录…")

        thread = threading.Thread(
            target=self._scan_worker, args=(path, generation, self.cancel_event),
            daemon=True,
        )
        thread.start()

    def _scan_worker(self, path, generation, cancel_event):
        """后台线程:计算每个直接子条目的大小。"""
        try:
            entries = list(os.scandir(path))
        except (OSError, PermissionError) as e:
            self.root.after(0, lambda: self._scan_failed(generation, str(e)))
            return

        results = []
        total_count = len(entries)
        for idx, entry in enumerate(entries):
            if cancel_event.is_set() or generation != self.scan_generation:
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_link = entry.is_symlink()
            except (OSError, PermissionError):
                is_dir = False
                is_link = False

            try:
                if is_dir and not is_link:
                    size = folder_size(entry.path, cancel_event)
                    type_label = "文件夹"
                elif is_link:
                    size = 0
                    type_label = "链接"
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    ext = os.path.splitext(entry.name)[1].lower()
                    type_label = ext[1:].upper() + " 文件" if ext else "文件"
            except (OSError, PermissionError):
                size = 0
                type_label = "无法访问"

            results.append({
                "name": entry.name,
                "path": entry.path,
                "size": size,
                "is_dir": is_dir and not is_link,
                "type": type_label,
            })

            # 周期性更新进度
            if idx % 20 == 0:
                self.root.after(
                    0, self._update_progress, generation, idx + 1, total_count
                )

        self.root.after(0, self._scan_done, generation, results, cancel_event.is_set())

    def _update_progress(self, generation, done, total):
        if generation == self.scan_generation:
            self.status_var.set(f"正在扫描… {done}/{total}")

    def _scan_failed(self, generation, message):
        if generation != self.scan_generation:
            return
        self.cancel_btn.config(state=tk.DISABLED)
        self.status_var.set(f"无法读取该目录:{message}")

    def _scan_done(self, generation, results, cancelled):
        if generation != self.scan_generation:
            return
        self._results = results
        self._render(results)
        self.cancel_btn.config(state=tk.DISABLED)

        total_size = sum(r["size"] for r in results)
        n_dirs = sum(1 for r in results if r["is_dir"])
        n_files = len(results) - n_dirs
        prefix = "已停止 — " if cancelled else ""
        self.status_var.set(
            f"{prefix}共 {len(results)} 项({n_dirs} 个文件夹, {n_files} 个文件)"
            f",合计 {human_size(total_size)}"
        )

    def _render(self, results):
        """根据当前排序设置渲染列表。"""
        self.tree.delete(*self.tree.get_children())
        self.item_data.clear()

        total_size = sum(r["size"] for r in results) or 1

        key = self.sort_column
        if key == "name":
            results = sorted(results, key=lambda r: r["name"].lower(),
                             reverse=self.sort_reverse)
        elif key == "type":
            results = sorted(results, key=lambda r: r["type"],
                             reverse=self.sort_reverse)
        else:  # size
            results = sorted(results, key=lambda r: r["size"],
                             reverse=self.sort_reverse)

        for r in results:
            percent = r["size"] / total_size * 100
            bar = "█" * int(percent / 5)  # 每格 5%
            icon = "📁 " if r["is_dir"] else "📄 "
            iid = self.tree.insert(
                "", tk.END,
                text=icon + r["name"],
                values=(
                    human_size(r["size"]),
                    r["type"],
                    f"{bar} {percent:.1f}%",
                ),
            )
            self.item_data[iid] = r

        # 选中第一项并聚焦列表,使方向键立即可用(类似资源管理器)
        children = self.tree.get_children()
        if children:
            first = children[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.tree.see(first)
            self.tree.focus_set()

    def _sort_by(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            # 大小默认从大到小,名称/类型默认从小到大
            self.sort_reverse = (column == "size")
        if hasattr(self, "_results"):
            self._render(self._results)


def main():
    root = tk.Tk()
    # 在 Windows 上让界面更清晰(高 DPI)
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    DiskUsageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
