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
        self.busy = False
        self.q = queue.Queue()

        self._build()
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
        ttk.Checkbutton(r1, text="自动分流（白底平坦图直接原图直出）",
                        variable=self.auto_route).pack(side="left", padx=14)

        out = ttk.LabelFrame(self.root, text="4. 输出到（原图绝不会被改动）")
        out.pack(fill="x", **pad)
        r2 = ttk.Frame(out); r2.pack(fill="x", padx=8, pady=6)
        ttk.Entry(r2, textvariable=self.output).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="换目录…", command=self.pick_out).pack(side="left", padx=6)
        ttk.Button(r2, text="打开", command=self.open_out).pack(side="left")

        run = ttk.Frame(self.root); run.pack(fill="x", **pad)
        self.btn = ttk.Button(run, text="开始处理", command=self.start)
        self.btn.pack(side="left")
        ttk.Button(run, text="只判定不出图", command=lambda: self.start(judge=True)).pack(
            side="left", padx=6)
        ttk.Button(run, text="跑自检", command=self.selftest).pack(side="left", padx=6)
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

    def _after_pick(self):
        files, is_dir = collect(self.inputs[0]) if len(self.inputs) == 1 else (self.inputs, False)
        if not files:
            self.lbl_in.config(text="这里没有图片")
            return
        if is_dir:
            self.lbl_in.config(text="文件夹：%d 张图" % len(files))
        else:
            self.lbl_in.config(text="已选 %d 张图" % len(files))
        if not self.output.get():
            base = os.path.basename(self.inputs[0].rstrip("\\/"))
            parent = os.path.dirname(self.inputs[0].rstrip("\\/")) or HERE
            self.output.set(os.path.join(parent, base + "_增强"))

    def pick_out(self):
        d = filedialog.askdirectory(title="输出目录")
        if d:
            self.output.set(d)

    def open_out(self):
        p = self.output.get()
        if p and os.path.isdir(p):
            os.startfile(p)
        else:
            messagebox.showinfo("提示", "还没有输出目录")

    def on_preset(self):
        p = E.apply_preset(self.preset.get())
        self.amount.set(p["amount"]); self.scale.set(p["scale"])
        self.lbl_amt.config(text="%.1f" % p["amount"])
        self.lbl_scl.config(text="%.1fx" % p["scale"])

    # ---------- 执行 ----------
    def start(self, judge=False):
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
        self.write("=" * 66)
        self.write("预设「%s」：%s" % (self.preset.get(), p["desc"]))
        self.write("共 %d 张，锐化 %.1f，放大 %.1fx，自动分流 %s"
                   % (len(files), self.amount.get(), self.scale.get(),
                      "开" if self.auto_route.get() else "关"))
        if judge:
            self.write("模式：只判定，不出图")
        else:
            self.write("输出：%s" % out)
        self.write("源图：只读，不会有任何改动")
        self.write("=" * 66)

        # ★ 所有 tkinter 变量的读取必须在主线程完成，再把**值**传进工作线程。
        #   tkinter 的变量不是线程安全的，在工作线程里 .get() 会抛
        #   "main thread is not in main loop"（实测踩过）。
        t = threading.Thread(
            target=self._work,
            args=(files, out, judge, dark, blur,
                  float(self.amount.get()), float(self.scale.get()),
                  bool(self.auto_route.get())),
            daemon=True)
        t.start()

    def _work(self, files, out, judge, dark, blur, amount, scale, auto):
        t0 = time.time(); n_run = n_skip = n_err = 0
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
            self.q.put(("log", "[%d/%d] %-8s %s  带内 %.3f  链路 %+.1f%%   %s"
                        % (i, len(files), v["verdict"], "增强" if do else "直出",
                           v["band_ratio"], v["hf_gain_chain"] * 100,
                           os.path.basename(p))))
            if not judge:
                rel = os.path.basename(p)
                dst = os.path.join(out, os.path.splitext(rel)[0] + "_enh.png")
                try:
                    if do:
                        src = E._read(p)
                        o, _m = E.enhance_one(src, amount, dark_gain=dark, blur_gain=blur)
                        E._write(dst, E.upscale(o, scale))
                    elif rel.lower().endswith(".png") and scale <= 1.0:
                        shutil.copy2(p, dst)          # 直出 = 逐字节原样
                    else:
                        E._write(dst, E.upscale(E._read(p), scale))
                except Exception as exc:              # noqa: BLE001
                    self.q.put(("log", "    写出失败：%s" % str(exc)[:80])); n_err += 1
                rows.append([rel, v["verdict"], "增强" if do else "直出",
                             "%.3f" % v["band_ratio"], "%.3f" % v["mask_cov"],
                             "%+.1f%%" % (v["hf_gain_chain"] * 100)])
            n_run += 1 if do else 0
            n_skip += 0 if do else 1
            self.q.put(("pb", i))
        if not judge:
            import csv
            rp = os.path.join(out, "_处理报告.csv")
            with open(rp, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerows(rows)
            self.q.put(("log", "报告 -> %s" % rp))
        dt = time.time() - t0
        self.q.put(("log", "-" * 66))
        self.q.put(("log", "完成：增强 %d / 直出 %d / 出错 %d，用时 %.1fs（%.2fs/张）"
                    % (n_run, n_skip, n_err, dt, dt / max(1, len(files)))))
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
                elif kind == "done":
                    self.busy = False
                    self.btn.config(state="normal")
                    self._save()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    # ---------- 设置持久化 ----------
    def _save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"preset": self.preset.get(), "amount": self.amount.get(),
                           "scale": self.scale.get(), "auto": self.auto_route.get(),
                           "output": self.output.get()}, f, ensure_ascii=False, indent=2)
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
            self.lbl_amt.config(text="%.1f" % self.amount.get())
            self.lbl_scl.config(text="%.1fx" % self.scale.get())
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
