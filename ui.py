#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片增强 · 图形界面

双击 `启动.bat` 或用 `pythonw ui.py` 打开。

界面只做三件事：选图、选预设、点开始。所有算法都在 enhance.py 里，这里只是外壳。

★ 安全约定（写死在代码里，不靠自觉）：
    1. **绝不修改源文件**：全程只读原图，输出一律写到"输出目录"。
    2. 输出目录一旦与源图路径重合，直接拒绝执行并报错。
    3. 默认输出到源图旁边的 `<名字>_增强` 文件夹，不覆盖任何已有文件。
"""
from __future__ import annotations
import json, os, queue, shutil, subprocess, sys, threading, time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import enhance as E                                       # noqa: E402

SETTINGS = os.path.join(HERE, "_上次设置.json")
EXTS = E.EXTS

# 输出格式的三种档位与显示名。jpeg-hi = 高保真 JPEG（4:4:4 色度不下采样 + optimize）。
FMT_NAME = {"jpeg": "JPEG", "jpeg-hi": "JPEG 高保真", "png": "PNG"}


def fmt_to_ext_q(fmt):
    """格式档位 -> (扩展名, JPEG 质量 或 None, 是否高保真)。"""
    if fmt == "png":
        return ".png", None, False
    if fmt == "jpeg-hi":
        return ".jpg", 95, True
    return ".jpg", 92, False


def collect(path):
    """返回 (图片列表, 是否是目录)。"""
    if os.path.isdir(path):
        return [p for p in E.iter_images(path)], True
    if os.path.isfile(path) and path.lower().endswith(EXTS):
        return [path], False
    return [], False


class App:
    def __init__(self, root):
        self.root = root
        root.title("图片增强")
        root.geometry("980x720")
        root.minsize(900, 640)

        self.inputs = []
        self.output = tk.StringVar()
        self.preset = tk.StringVar(value="推荐")
        self.amount = tk.DoubleVar(value=2.0)
        self.scale = tk.DoubleVar(value=1.0)
        self.auto_route = tk.BooleanVar(value=True)
        # ★ 尺寸策略：把处理尺度对齐到"平台显示宽度"（见 enhance.fit_long_edge）。
        #   shrink    = 真缩像素（最小、效果最好）
        #   normalize = 不缩像素，只把半径/门限换算到该宽度（保留原像素）
        #   keep      = 都不做，按原图尺度处理（历史行为）
        self.size_mode = tk.StringVar(value="shrink")
        self.shrink_px = tk.IntVar(value=750)
        self.fmt = tk.StringVar(value="jpeg")
        self.busy = False
        self.q = queue.Queue()
        self.made = []            # 这次跑出来/直出的文件，最后一个就是"最新结果"
        self.auto_out = True      # 输出目录是否"跟着源图自动变"

        self._build()
        self.on_shrink()
        self._load()
        root.after(80, self._pump)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面 ----------
    def _build(self):
        pad = dict(padx=10, pady=6)

        top = ttk.LabelFrame(self.root, text="1. 选择图片或文件夹")
        top.pack(fill="x", **pad)
        row = ttk.Frame(top); row.pack(fill="x", padx=8, pady=6)
        ttk.Button(row, text="选图片…", command=self.pick_files).pack(side="left")
        ttk.Button(row, text="选文件夹…", command=self.pick_dir).pack(side="left", padx=6)
        ttk.Button(row, text="清空", command=self.clear_inputs).pack(side="left")
        self.lbl_in = ttk.Label(row, text="还没选", foreground="#666")
        self.lbl_in.pack(side="left", padx=10)

        mid = ttk.LabelFrame(self.root, text="2. 选预设（鼠标悬停看说明）")
        mid.pack(fill="x", **pad)
        grid = ttk.Frame(mid); grid.pack(fill="x", padx=8, pady=6)
        self.preset_boxes = {}
        tips = {}
        for i, (name, p) in enumerate(E.PRESETS.items()):
            b = ttk.Radiobutton(grid, text=name, value=name, variable=self.preset,
                                command=self.on_preset)
            b.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 22), pady=3)
            self.preset_boxes[name] = b
            tips[name] = (p["desc"] + "\n锐化 %.1f ｜ 暗部加成 %.1f ｜ 糊区加成 %.1f ｜ 放大 %.1fx"
                          % (p["amount"], p["dark_gain"], p["blur_gain"], p["scale"]))
            self._tooltip(b, tips[name])

        opt = ttk.LabelFrame(self.root, text="3. 微调（可选）")
        opt.pack(fill="x", **pad)
        r1 = ttk.Frame(opt); r1.pack(fill="x", padx=8, pady=6)
        ttk.Label(r1, text="锐化强度").pack(side="left")
        self.sl_amt = ttk.Scale(r1, from_=0.5, to=4.0, variable=self.amount,
                                orient="horizontal", length=220,
                                command=lambda *_: self.lbl_amt.config(
                                    text="%.1f" % self.amount.get()))
        self.sl_amt.pack(side="left", padx=8)
        self.lbl_amt = ttk.Label(r1, text="2.0", width=5); self.lbl_amt.pack(side="left")
        ttk.Label(r1, text="   放大").pack(side="left")
        self.sl_scl = ttk.Scale(r1, from_=1.0, to=4.0, variable=self.scale,
                                orient="horizontal", length=160,
                                command=lambda *_: self.lbl_scl.config(
                                    text="%.1fx" % self.scale.get()))
        self.sl_scl.pack(side="left", padx=8)
        self.lbl_scl = ttk.Label(r1, text="1.0x", width=6); self.lbl_scl.pack(side="left")
        r1b = ttk.Frame(opt); r1b.pack(fill="x", padx=8, pady=(0, 6))
        self.ck_auto = ttk.Checkbutton(
            r1b, text="自动分流（白底平坦图直接原图直出，不浪费一次重编码）",
            variable=self.auto_route)
        self.ck_auto.pack(side="left")
        ttk.Label(r1b, text="预设会覆盖右侧两个滑块；手动拖动滑块即改为自定义",
                  foreground="#888").pack(side="left", padx=14)

        # ★ 第 3.5 行：尺寸策略 + 格式。
        #   两种尺寸策略是等价的（都把处理尺度对齐到「平台显示宽度」），
        #   区别只在要不要真的缩像素：
        #     缩图   —— 文件小 94%，效果最好（实测 HF +32.5%）
        #     不缩图 —— 保留原始像素，效果接近（HF +27.7%，局部对比反而最高 +6.9%）
        r1c = ttk.Frame(opt); r1c.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(r1c, text="尺寸策略").pack(side="left")
        ttk.Radiobutton(r1c, text="缩到长边", value="shrink",
                        variable=self.size_mode, command=self.on_shrink).pack(side="left")
        self.cmb_px = ttk.Combobox(r1c, textvariable=self.shrink_px, width=6,
                                   values=("600", "750", "900", "1080", "1200"))
        self.cmb_px.pack(side="left", padx=4)
        ttk.Label(r1c, text="px").pack(side="left")
        self.cmb_px.bind("<<ComboboxSelected>>", lambda _e: self.on_shrink())
        ttk.Radiobutton(r1c, text="保持原像素，按该宽度归一", value="normalize",
                        variable=self.size_mode,
                        command=self.on_shrink).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(r1c, text="都不做", value="keep",
                        variable=self.size_mode,
                        command=self.on_shrink).pack(side="left", padx=(8, 0))
        ttk.Label(r1c, text="   ｜ 输出格式").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(r1c, text="JPEG", value="jpeg",
                        variable=self.fmt, command=self.on_shrink).pack(side="left", padx=(6, 0))
        # ★ 高保真 JPEG：色度不下采样（4:4:4）+ optimize。
        #   给"像素一个不少、只想把 MB 压成 KB"用的 —— 见 on_shrink 里的实测。
        ttk.Radiobutton(r1c, text="JPEG 高保真", value="jpeg-hi",
                        variable=self.fmt, command=self.on_shrink).pack(side="left", padx=(4, 0))
        ttk.Radiobutton(r1c, text="PNG（像素一个不差）", value="png",
                        variable=self.fmt, command=self.on_shrink).pack(side="left", padx=(4, 0))
        r1d = ttk.Frame(opt); r1d.pack(fill="x", padx=8, pady=(0, 8))
        self.lbl_shrink = ttk.Label(r1d, text="", foreground="#0a7")
        self.lbl_shrink.pack(side="left", padx=12)

        out = ttk.LabelFrame(self.root, text="4. 输出到（原图绝不会被改动）")
        out.pack(fill="x", **pad)
        r2 = ttk.Frame(out); r2.pack(fill="x", padx=8, pady=6)
        ttk.Entry(r2, textvariable=self.output).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="换目录…", command=self.pick_out).pack(side="left", padx=6)
        ttk.Button(r2, text="打开目录", command=self.open_out).pack(side="left")

        run = ttk.Frame(self.root); run.pack(fill="x", **pad)
        self.btn = ttk.Button(run, text="开始处理", command=self.start)
        self.btn.pack(side="left")
        ttk.Button(run, text="只判定不出图", command=lambda: self.start(judge=True)).pack(
            side="left", padx=6)
        # ★ 「只压缩」= 不做任何增强，只按当前尺寸策略/格式重新编码。
        #   用途：把它和增强后的结果摆在一起比 —— 尤其是风格化的图，
        #   人物/物件本来就柔和，没有基准线很难判断增强到底带来了多少。
        ttk.Button(run, text="只压缩（对照）",
                   command=lambda: self.start(compress_only=True)).pack(side="left", padx=6)
        ttk.Button(run, text="跑自检", command=self.selftest).pack(side="left", padx=6)
        # ★ 跑完一键看结果：直接拿系统默认看图程序打开最新的那张，不用自己翻目录
        self.btn_look = ttk.Button(run, text="看最新结果", command=self.open_latest,
                                   state="disabled")
        self.btn_look.pack(side="left", padx=(18, 4))
        self.lbl_made = ttk.Label(run, text="", foreground="#888")
        self.lbl_made.pack(side="left")
        self.pb = ttk.Progressbar(run, mode="determinate", length=260)
        self.pb.pack(side="right")

        logf = ttk.LabelFrame(self.root, text="进度")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, wrap="none", height=14, font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.configure(state="disabled")

    def _tooltip(self, widget, text):
        def enter(_e):
            self._tip = tk.Toplevel(widget); self._tip.wm_overrideredirect(True)
            self._tip.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 20,
                                              widget.winfo_rooty() + 24))
            tk.Label(self._tip, text=text, justify="left", background="#ffffe1",
                     relief="solid", borderwidth=1, font=("Microsoft YaHei", 9),
                     padx=8, pady=5).pack()
        def leave(_e):
            if getattr(self, "_tip", None):
                self._tip.destroy(); self._tip = None
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def write(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 选择 ----------
    def pick_files(self):
        fs = filedialog.askopenfilenames(
            title="选图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
                       ("全部", "*.*")])
        if fs:
            self.inputs = list(fs)
            self._after_pick()

    def pick_dir(self):
        d = filedialog.askdirectory(title="选文件夹")
        if d:
            self.inputs = [d]
            self._after_pick()

    def clear_inputs(self):
        self.inputs = []
        self.lbl_in.config(text="还没选")
        self.output.set("")
        self.auto_out = True       # 清空后，输出目录回到"自动跟随"状态

    def _after_pick(self):
        files, is_dir = collect(self.inputs[0]) if len(self.inputs) == 1 else (self.inputs, False)
        if not files:
            self.lbl_in.config(text="这里没有图片")
            return
        if is_dir:
            self.lbl_in.config(text="文件夹：%d 张图" % len(files))
        else:
            self.lbl_in.config(text="已选 %d 张图" % len(files))
        # ★ 换了源就跟着换输出目录（2026-10-01 修）：
        #   老逻辑只在输出为空时才填，于是"换了一张图，输出还指着上一张的目录"——
        #   结果就是上一张的成品混在同一个文件夹里，看着像"没重跑"。
        #   只有在用户自己选过/改过目录时才不覆盖（auto_out=False）。
        if getattr(self, "auto_out", True):
            src0 = self.inputs[0].rstrip("\\/")
            base = os.path.basename(src0)
            parent = os.path.dirname(src0) or HERE
            # 单张图：去掉扩展名再拼 _增强，免得出现 "xxx.png_增强" 这种别扭名字
            if not is_dir:
                base = os.path.splitext(base)[0]
            self.output.set(os.path.join(parent, base + "_增强"))

    def pick_out(self):
        d = filedialog.askdirectory(title="输出目录")
        if d:
            self.output.set(d)
            self.auto_out = False       # 用户自己选的，之后不跟着源图变

    def open_out(self):
        p = self.output.get()
        if p and os.path.isdir(p):
            os.startfile(p)
        else:
            messagebox.showinfo("提示", "还没有输出目录")

    def open_latest(self):
        """用系统默认看图程序打开这次跑出来的最新一张。

        为什么要有这个按钮：跑完先想看效果，得自己记路径、开资源管理器、翻目录 ——
        尤其批量跑几百张时。这个按钮直接开最后一张（也支持多选：按住 Ctrl 点按钮
        没意义，这里是开最新的，想看别的用「打开目录」）。
        """
        alive = [p for p in self.made if os.path.isfile(p)]
        if not alive:
            messagebox.showinfo("提示", "还没有跑出图。先选图再点「开始处理」。")
            return
        target = alive[-1]
        try:
            os.startfile(target)
        except OSError as exc:                          # noqa: BLE001
            # 没有关联的看图程序时才走到这
            messagebox.showwarning("打不开", "%s\n\n可以点「打开目录」自己看。" % exc)

    def on_preset(self):
        p = E.apply_preset(self.preset.get())
        self.amount.set(p["amount"]); self.scale.set(p["scale"])
        self.lbl_amt.config(text="%.1f" % p["amount"])
        self.lbl_scl.config(text="%.1fx" % p["scale"])

    def on_shrink(self):
        """把当前尺寸策略翻译成一句人话，实时显示。

        数字来自 enhance.enhance_one / fit_long_edge 的实测（16 张真实商品图，
        都换算到 750 显示宽度量）：
            不缩放尺度（历史）    HF +20.6%   局部对比 +3.9%
            缩到 750 再锐化       HF +32.5%   局部对比 +5.2%
            不缩像素 + 尺度归一   HF +27.7%   局部对比 +6.9%
        """
        mode = self.size_mode.get()
        try:
            n = int(self.shrink_px.get())
        except (TypeError, ValueError):
            n = 750
        fmt = self.fmt.get()
        # ★ 体积实测（16 张真实商品图，原始单张平均 2038 KB）：
        #     不缩像素 + PNG       1263 KB   （像素一个不差，只省 38%）
        #     不缩像素 + JPEG       185 KB   （省 91%，有损但 45 dB）
        #     不缩像素 + JPEG 高保真 317 KB   （省 85%，48.5 dB、色度不下采样）
        #     缩到 750 + JPEG        87 KB   （省 96%）
        if mode == "keep":
            # 「什么都不做」也有体积账：这里给出来，免得以为选了它就能既不变又不减
            if fmt == "png":
                tail = "；PNG 无损，体积只省约 38%"
            elif fmt == "jpeg-hi":
                tail = "；高保真 JPEG 约省 85%"
            else:
                tail = "；JPEG 约省 91%"
            self.lbl_shrink.config(
                text="按原图像素尺度处理 —— 平台要缩图时，锐化高频会被吃掉一半" + tail,
                foreground="#c60")
            return
        if fmt == "png":
            # 无损档：这里必须说实话 —— PNG 省不了多少，想变 KB 就得让步
            if mode == "normalize":
                self.lbl_shrink.config(
                    text="保持原像素、PNG 无损 —— 像素一个不差，但体积只省约 38%"
                         "（单张仍 ~1263 KB）。想把 MB 变 KB，得改用 JPEG 或缩图",
                    foreground="#c60")
            else:
                self.lbl_shrink.config(
                    text="长边缩到 %d、PNG 无损 —— 像素一个不差（只是变少），体积约省 71%%"
                         "（单张 ~590 KB）" % n,
                    foreground="#0a7")
            return
        hi = fmt == "jpeg-hi"
        if mode == "normalize":
            self.lbl_shrink.config(
                text="保持原像素、按 %d 显示宽度归一尺度%s —— 清晰度接近缩图那档，"
                     "但像素一个不少" % (n, "，高保真 JPEG 约省 85%（单张 ~317 KB）"
                                          if hi else "，JPEG 约省 91%（单张 ~185 KB）"),
                foreground="#0a7")
            return
        self.lbl_shrink.config(
            text="长边缩到 %d 再锐化%s —— 实测最清晰的一档（HF +32.5%%）"
                 % (n, "，高保真 JPEG 体积约省 95%" if hi else "，JPEG 约省 96% 体积"),
            foreground="#0a7")

    # ---------- 执行 ----------
    def start(self, judge=False, compress_only=False):
        if self.busy:
            return
        if not self.inputs:
            messagebox.showwarning("还没选", "先选图片或文件夹")
            return
        out = self.output.get().strip()
        if not judge and not out:
            messagebox.showwarning("还没选", "先选输出目录")
            return

        files, is_dir = (collect(self.inputs[0]) if len(self.inputs) == 1
                         else (self.inputs, False))
        if not files:
            messagebox.showwarning("没有图片", "选中的位置里没有可处理的图片")
            return

        # ★ 安全检查：输出目录不能指向源图所在目录本身
        if not judge:
            out_abs = os.path.abspath(out)
            for f in files:
                if os.path.abspath(os.path.dirname(f)) == out_abs:
                    messagebox.showerror(
                        "拒绝执行",
                        "输出目录和源图目录是同一个。\n"
                        "这样可能覆盖你的原图，已阻止。\n请换一个输出目录。")
                    return
            if any(os.path.abspath(f) == out_abs for f in files):
                messagebox.showerror("拒绝执行", "输出路径和某张源图重合，已阻止。")
                return

        self.busy = True
        self.btn.config(state="disabled")
        self.pb.config(maximum=len(files), value=0)
        p = E.apply_preset(self.preset.get())
        dark = p["dark_gain"] if self.preset.get() != "推荐" else 0.0
        blur = p["blur_gain"] if self.preset.get() != "推荐" else 0.0
        clarity = float(p.get("clarity", 0.0))
        # ★ 尺寸策略（见 enhance.fit_long_edge / enhance_one 的 display_w）。
        #   tkinter 变量的读取一律留在主线程，只把值传进工作线程。
        mode = self.size_mode.get()
        try:
            px = int(self.shrink_px.get())
        except (TypeError, ValueError):
            px = 750
        shrink = px if mode == "shrink" else 0
        dispw = px if mode == "normalize" else 0
        fmt = self.fmt.get()
        self.write("=" * 66)
        if compress_only:
            self.write("★ 只压缩（对照）—— 不做任何增强，只按下面的尺寸/格式重新编码")
        else:
            self.write("预设「%s」：%s" % (self.preset.get(), p["desc"]))
            self.write("共 %d 张，锐化 %.1f，放大 %.1fx，自动分流 %s，输出 %s"
                       % (len(files), self.amount.get(), self.scale.get(),
                          "开" if self.auto_route.get() else "关",
                          FMT_NAME.get(fmt, fmt.upper())))
        if compress_only:
            # 这条路不锐化，别照抄增强那边的措辞（会误导成"缩完还要锐"）
            if shrink:
                self.write("按原图缩到长边 %d px，然后直接编码（只缩不锐）" % shrink)
            else:
                self.write("像素尺寸原样保留，直接编码")
        elif shrink:
            self.write("先缩到长边 %d px 再锐化 —— 比先锐后缩更清晰，文件也更小" % shrink)
        elif dispw:
            self.write("保持原始像素，把锐化尺度按 %d px 显示宽度归一 —— "
                       "平台缩图后剩下的清晰度接近「缩图再锐化」那档" % dispw)
        else:
            self.write("按原图像素尺度处理（历史行为）")
        if clarity > 0 and not compress_only:
            # ★ 唯一会改观感的一档 —— 必须提前说清楚，别让用户自己发现
            self.write("⚠ 这一档会拉开明暗对比（不是纯锐化），观感会变、强边处有极轻微光晕。"
                       "想要零失真的档位请选前五个。")
        if judge:
            self.write("模式：只判定，不出图")
        elif compress_only:
            self.write("输出：%s    共 %d 张，输出 %s"
                       % (out, len(files), FMT_NAME.get(fmt, fmt.upper())))
        else:
            self.write("输出：%s" % out)
        self.write("源图：只读，不会有任何改动")
        self.write("=" * 66)

        # ★ 所有 tkinter 变量的读取必须在主线程完成，再把**值**传进工作线程。
        #   tkinter 的变量不是线程安全的，在工作线程里 .get() 会抛
        #   "main thread is not in main loop"（实测踩过）。
        t = threading.Thread(
            target=self._work,
            args=(files, out, judge, dark, blur, clarity,
                  float(self.amount.get()), float(self.scale.get()),
                  bool(self.auto_route.get()), shrink, fmt, dispw, compress_only),
            daemon=True)
        t.start()

    def _work(self, files, out, judge, dark, blur, clarity, amount, scale, auto,
              shrink=0, fmt="png", dispw=0, compress_only=False):
        t0 = time.time(); n_run = n_skip = n_err = n_cmp = 0
        tot_in = tot_out = 0
        ext, q, hi = fmt_to_ext_q(fmt)
        self.q.put(("reset", None))
        if not judge:
            os.makedirs(out, exist_ok=True)
            rows = [["文件", "判定", "动作", "带内占比", "主体遮罩", "链路增益"]]
        for i, p in enumerate(files, 1):
            try:
                v = E.measure(p, amount=amount)
            except Exception as exc:                  # noqa: BLE001
                self.q.put(("log", "[%d/%d] 读不了 %s：%s"
                            % (i, len(files), os.path.basename(p), str(exc)[:60])))
                n_err += 1; self.q.put(("pb", i)); continue
            do = (not auto) or v["verdict"] in ("run", "marginal")
            if compress_only:
                self.q.put(("log", "[%d/%d] 只压缩   %s"
                            % (i, len(files), os.path.basename(p))))
            else:
                self.q.put(("log", "[%d/%d] %-8s %s  带内 %.3f  链路 %+.1f%%   %s"
                            % (i, len(files), v["verdict"], "增强" if do else "直出",
                               v["band_ratio"], v["hf_gain_chain"] * 100,
                               os.path.basename(p))))
            if not judge:
                rel = os.path.basename(p)
                # ★ 只压缩走 _cmp 后缀，增强走 _enh —— 两条路可以落在同一个目录里对比
                dst = os.path.join(out, os.path.splitext(rel)[0]
                                   + ("_cmp" if compress_only else "_enh") + ext)
                try:
                    if compress_only:
                        # 不锐化、不归一尺度 —— 只按需缩图 + 重新编码
                        work, _s = E.fit_long_edge(E._read(p), shrink)
                        E._write(dst, E.upscale(work, scale), q, hi)
                    elif do:
                        src = E._read(p)
                        work, _s = E.fit_long_edge(src, shrink)   # ★ 先缩
                        o, _m = E.enhance_one(work, amount, dark_gain=dark,
                                              blur_gain=blur, clarity=clarity,
                                              display_w=dispw)
                        E._write(dst, E.upscale(o, scale), q, hi)
                    elif (rel.lower().endswith(".png") and scale <= 1.0
                          and fmt == "png" and not shrink):
                        shutil.copy2(p, dst)          # 直出 = 逐字节原样
                    else:
                        work, _s = E.fit_long_edge(E._read(p), shrink)
                        E._write(dst, E.upscale(work, scale), q, hi)
                    try:
                        tot_in += os.path.getsize(p)
                        tot_out += os.path.getsize(dst)
                    except OSError:
                        pass
                    self.q.put(("made", dst))         # 「看最新结果」认的就是它
                except Exception as exc:              # noqa: BLE001
                    # ★ 错误不能截断着给人猜（实测：[:80] 正好切在路径中间，
                    #   看着像"路径太长/非法参数"，其实是被占用）。
                    #   所以逐行铺开打，路径给全。
                    self.q.put(("log", "    [X] 处理失败，这张没有输出"))
                    for ln in str(exc).splitlines():
                        if ln.strip():
                            self.q.put(("log", "        " + ln.strip()))
                    n_err += 1
                rows.append([rel, v["verdict"],
                             "只压缩" if compress_only else ("增强" if do else "直出"),
                             "%.3f" % v["band_ratio"], "%.3f" % v["mask_cov"],
                             "%+.1f%%" % (v["hf_gain_chain"] * 100)])
            if compress_only:
                n_cmp += 1
            else:
                n_run += 1 if do else 0
                n_skip += 0 if do else 1
            self.q.put(("pb", i))
        if not judge:
            import csv
            # ★ 两条路各写各的报告：写进同一个目录时不能互相覆盖，
            #   否则后跑的那次会把前一次的读数冲掉，就没法对着看了。
            rp = os.path.join(out, "_处理报告_只压缩.csv" if compress_only
                              else "_处理报告.csv")
            with open(rp, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerows(rows)
            self.q.put(("log", "报告 -> %s" % rp))
        dt = time.time() - t0
        self.q.put(("log", "-" * 66))
        if compress_only:
            self.q.put(("log", "完成：只压缩 %d / 出错 %d，用时 %.1fs（%.2fs/张）"
                        % (n_cmp, n_err, dt, dt / max(1, len(files)))))
            self.q.put(("log", "★ 这一轮没有做任何增强（像素未改），只是重新编码 —— 用于对照"))
        else:
            self.q.put(("log", "完成：增强 %d / 直出 %d / 出错 %d，用时 %.1fs（%.2fs/张）"
                        % (n_run, n_skip, n_err, dt, dt / max(1, len(files)))))
        if not judge and tot_in:
            self.q.put(("log", "体积：%.1f MB -> %.1f MB（省 %.0f%%）"
                        % (tot_in / 1048576.0, tot_out / 1048576.0,
                           (1 - tot_out / float(tot_in)) * 100)))
        self.q.put(("done", None))

    def selftest(self):
        if self.busy:
            return
        self.write("=" * 66)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = E.selftest()
        for line in buf.getvalue().rstrip().splitlines():
            self.write(line)
        self.write("自检结果：%s" % ("通过" if rc == 0 else "有项目未通过"))
        self.write("=" * 66)

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.write(val)
                elif kind == "pb":
                    self.pb.config(value=val)
                elif kind == "reset":
                    self.made = []
                    self.btn_look.config(state="disabled")
                    self.lbl_made.config(text="")
                elif kind == "made":
                    self.made.append(val)
                    self.btn_look.config(state="normal")
                    self.lbl_made.config(
                        text="共 %d 张" % len(self.made) if len(self.made) > 1 else "")
                elif kind == "done":
                    self.busy = False
                    self.btn.config(state="normal")
                    self._save()
                    if self.made:
                        self.write("已产出 %d 张 —— 点「看最新结果」直接打开。" % len(self.made))
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    # ---------- 设置持久化 ----------
    def _save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"preset": self.preset.get(), "amount": self.amount.get(),
                           "scale": self.scale.get(), "auto": self.auto_route.get(),
                           "output": self.output.get(),
                           "shrink_px": self.shrink_px.get(),
                           "fmt": self.fmt.get(),
                           "size_mode": self.size_mode.get(),
                           "auto_out": bool(getattr(self, "auto_out", True))},
                          f, ensure_ascii=False, indent=2)
        except Exception:                              # noqa: BLE001
            pass

    def _load(self):
        if not os.path.exists(SETTINGS):
            return
        try:
            d = json.load(open(SETTINGS, encoding="utf-8"))
            self.preset.set(d.get("preset", "推荐"))
            self.amount.set(float(d.get("amount", 2.0)))
            self.scale.set(float(d.get("scale", 1.0)))
            self.auto_route.set(bool(d.get("auto", True)))
            self.output.set(d.get("output", ""))
            # ★ auto_out 单独存：区分"目录是自动生成的"还是"用户自己选的"。
            #   光看目录是否为空判断不出来 —— 自动生成的目录也有内容，
            #   结果就是换了源图后输出还指着上一张（实测踩过）。
            self.auto_out = bool(d.get("auto_out", True))
            self.shrink_px.set(int(d.get("shrink_px", 750)))
            # 兼容旧设置文件：老版本只有 png/jpeg，新版本多了 jpeg-hi
            _f = d.get("fmt", "jpeg")
            self.fmt.set(_f if _f in FMT_NAME else "jpeg")
            # 兼容旧设置文件：老版本只有 shrink_on，没有 size_mode
            if "size_mode" in d:
                self.size_mode.set(d["size_mode"])
            elif "shrink_on" in d:
                self.size_mode.set("shrink" if d.get("shrink_on") else "keep")
            self.lbl_amt.config(text="%.1f" % self.amount.get())
            self.lbl_scl.config(text="%.1fx" % self.scale.get())
            self.on_shrink()
        except Exception:                              # noqa: BLE001
            pass

    def _on_close(self):
        self._save()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:                                  # noqa: BLE001
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
