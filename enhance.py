#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片增强 —— 单文件，纯 CPU，无模型、无权重、无显卡要求。

它做什么：
    1. 按图自动判断值不值得处理（白底平坦图直接用原图，不给平台二次压缩添乱）
    2. 门限保边锐化：只锐有结构的边缘，平坦区增益严格为 0
    3. 分区：人物/主体区锐化，暗且中性的物件区保持原样
    4. 批量：扫一个目录，跑的跑、跳的跳过，附 CSV 报告

它不做什么（这些都是刻意的）：
    不重建、不重绘、不补细节、不改颜色、不动全局曲线、不生成内容。
    只动锐度，且只在有结构的地方动。

用法：
    python enhance.py -i 图.jpg -o out\\               # 单张（全自动分流）
    python enhance.py -i 图目录 -o out\\               # 批量
    python enhance.py -i 图.jpg -o out\\ --report      # 附各亮度档读数
    python enhance.py -i 图.jpg -o out\\ --force       # 不做分流，一律处理
    python enhance.py -i 图.jpg --judge                # 只判定，不出图
    python enhance.py --selftest                       # 自检（不读外部图）

依赖：Python 3 + numpy + opencv-python（本机已装）
"""
from __future__ import annotations
import argparse, csv, os, shutil, sys, time
import cv2
import numpy as np

__version__ = "1.0"

# ============================================================================
# 0. 基础工具
# ============================================================================

def _read(path):
    """读图（cv2.imread 在 Windows 下读不了中文路径）。"""
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise IOError("读不了图: " + path)
    return img


def _write(path, bgr, quality=None, hi=False):
    """写图（同上，支持中文路径）。

    quality=None ⇒ PNG（无损，像素一个不差）；
    给了数值     ⇒ JPEG，质量 1~100；hi=True 时再加 4:4:4 + optimize（高保真档）。

    ★ hi 这一档是给"我不想缩像素，只想把 MB 压成 KB"用的（2026-10-01 实测）：
        q92 默认（4:2:0 色度下采样）：单张 185 KB，PSNR 45.5 dB，色度 MAE 0.52
        q95 + 4:4:4 + optimize：      单张 317 KB，PSNR 48.5 dB，色度 MAE 0.34
      ⇒ 色度不再被下采样，红/粉/金这类颜色边缘不会被抹开，
        代价是体积比默认档大一倍左右。

    ★ 为什么不用 numpy 的 buf.tofile(path)（实战踩过，2026-10-01）：
        任务栏里看到「写出失败：[Errno 22] Invalid argument: 'E:/xm/…'」，
        可去目录一看 —— **文件明明写出来了**，而且内容完好。
        原因：`ndarray.tofile(str)` 走的是 C 的 fopen，出错时的表现很不友好：
          - 目标文件被别的程序占着（最常见：你正开着看图软件看上一张）⇒ 报 22；
          - 路径里正斜杠与反斜杠混用、含中文时，边界情况还会再放大；
          - 而且它**先截断再写**，写到一半失败会留下半个文件（比报错更糟）。
        改成本文件的写法：原生 open 写**临时文件**，写完整了再 os.replace 顶替。
        好处有三：占用时不会毁掉旧文件、不会留半截文件、报错是真报错。

    ★ 真占用了怎么认出来：
        Windows 上目标被占用会抛 PermissionError(13) 或 OSError(22)；
        两种都翻译成人话再往上抛，别让用户对着一串 errno 猜。
    """
    ext = os.path.splitext(path)[1] or ".png"
    if quality is None:
        params = []
    elif hi:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality),
                  int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR),
                  int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444),
                  int(cv2.IMWRITE_JPEG_OPTIMIZE), 1]
    else:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    ok, buf = cv2.imencode(ext, bgr, params)
    if not ok:
        raise IOError("编码失败（扩展名 %s）：%s" % (ext, path))
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".writing"
    try:
        with open(tmp, "wb") as f:
            f.write(memoryview(buf))
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)          # 失败时不留垃圾
        except OSError:
            pass
        if exc.errno in (13, 22) or isinstance(exc, PermissionError):
            raise IOError(
                "写不进去（文件被占用？先关掉正在看这张图的程序再试）：\n    " + path
            ) from exc
        raise
    return path


def _smoothstep(x, a, b):
    """0..1 平滑过渡：a 以下恒 0，b 以上恒 1。"""
    t = np.clip((x - a) / max(b - a, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _hf(bgr):
    """高频能量（拉普拉斯绝对均值），ksize 固定 3。

    全程必须用同一个 ksize：ksize=1 与 ksize=3 读数能差 10% 以上，
    混用会让「提升了多少」这个结论没法比。
    """
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(np.abs(cv2.Laplacian(g, cv2.CV_32F, ksize=3)).mean())


# ============================================================================
# 1. 导引滤波（He et al.）：把图分成 base（大结构）+ detail（细纹理）
# ============================================================================

def guided_filter(I, p, r=3, eps=0.0025):
    """保边分解。比起高斯模糊，它不会把边缘两侧糊到一起，
    所以锐化后不会在边缘画出白边/黑边。"""
    k = (r, r)
    mI = cv2.boxFilter(I, cv2.CV_32F, k)
    mp = cv2.boxFilter(p, cv2.CV_32F, k)
    mIp = cv2.boxFilter(I * p, cv2.CV_32F, k)
    cov = mIp - mI * mp
    mII = cv2.boxFilter(I * I, cv2.CV_32F, k)
    var = mII - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    return cv2.boxFilter(a, cv2.CV_32F, k) * I + cv2.boxFilter(b, cv2.CV_32F, k)


# ============================================================================
# 2. 分区遮罩：暗 且 中性灰 = 物件区（包 / 裤 / 黑织物）
# ============================================================================

def object_mask(bgr, y_max=0.85, sat_gate=0.13, sat_full=0.50,
                feather=9, block=24, block_thresh=0.30):
    """返回 0..1 的物件遮罩。

    为什么用 HSV 的 S 当色门，而不是 YCrCb 距离：
        同一个黑包，YCrCb 距离 0.067 与皮肤 0.158 只差 2.4 倍；
        HSV-S 是 0.084 与 0.654，差 7.8 倍 —— 后者才能把门限卡在中间而不误伤皮肤。

    y_max 为什么能开到 0.85：
        包面在环境光下有彩色反射（实测部分区域 S 高达 0.5），紧的饱和度门
        会把那部分包判成非物件。放宽到 (0.13, 0.50) 后，包主体覆盖
        0.72 -> 0.96~1.00，而脸因为亮且高饱和仍被挡在门外（0.04）。

    block 为什么必要：
        逐像素判据会在包里留孔洞（高光、褶皱处亮度/饱和度越界），
        实测包芯覆盖只有 0.72 —— 剩下 28% 的包仍会被当成人物去锐化。
        先在 block x block 块上统计再上采样，孔洞自动填平（包芯 0.92）。
        比形态学闭运算/膨胀好：后者填洞时会把脸/耳环一起拉进来（脸 0.02 -> 0.19）。
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    S = hsv[..., 1] / 255.0
    Y = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)[..., 0] / 255.0
    lum_w = 1.0 - _smoothstep(Y, 0.0, y_max)
    sat_w = 1.0 - _smoothstep(S, sat_gate, sat_full)
    m = np.clip(lum_w * sat_w, 0.0, 1.0)
    if block and block > 1:
        h, w = m.shape
        hh, ww = h // block, w // block
        if hh < 1 or ww < 1:
            # 图比块还小（缩略图/图标）—— 块统计没有意义，直接返回
            return np.clip(m, 0.0, 1.0)
        blk = m[:hh * block, :ww * block].reshape(hh, block, ww, block).mean(axis=(1, 3))
        blk = _smoothstep(blk, block_thresh, min(1.0, block_thresh + 0.35))
        m = cv2.resize(blk.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    if feather > 0:
        f = feather | 1
        m = cv2.GaussianBlur(m, (f, f), 0)
    return np.clip(m, 0.0, 1.0)


# ============================================================================
# 3. 门限保边锐化：这个工具的核心
# ============================================================================

def sharpen_edges(bgr, amount=2.0, radius=3, eps=0.0025,
                  t0=0.045, t1=0.20, tol=0.004, soft=0.0):
    """只锐化有结构的地方，平坦区增益严格为 0。

    t0 / t1 —— 为什么必须有绝对门限：
        早期版本用 (0.35 + 0.65*w) 这种「最低也给 35% 增益」的权重，
        结果天空高频 +141%、橙墙 +90% —— 平坦区被硬生生画出爬纹。
        这是真正的失真源，不是调参能忍的。
        改成 smoothstep(t0, t1) 后，低于 t0 的平坦区增益恒等于 0，
        实测天空 +0%（旧版 +141%）。天空和墙面是判断爬纹的最佳试纸。

    tol —— 过冲钳制：
        结果被钳在 3x3 邻域 [min-tol, max+tol] 内，消掉白边和黑边。

    soft —— 细节软阈值（只在物件支线用）：
        |detail| < soft 的部分先削掉再放大。AI 生成图的细颗粒正落在这一档，
        而褶皱、扣子这类真实结构不受影响。
    """
    if amount <= 0:
        return bgr      # 早退：BGR->YCrCb->BGR 往返会掉 1 个色阶，amount=0 必须是严格恒等
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    Y = ycc[..., 0] / 255.0
    base = guided_filter(Y, Y, radius, eps)
    detail = Y - base
    if soft > 0:
        a = np.abs(detail)
        detail = detail * _smoothstep(a, soft, 2.0 * soft)
    gx = cv2.Sobel(Y, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(Y, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    w = _smoothstep(mag, t0, t1)
    Y2 = Y + detail * amount * w
    if tol > 0:
        k = np.ones((3, 3), np.float32)
        Y2 = np.clip(Y2, cv2.erode(Y, k) - tol, cv2.dilate(Y, k) + tol)
    ycc[..., 0] = np.clip(Y2, 0.0, 1.0) * 255.0
    return cv2.cvtColor(ycc.astype(np.uint8), cv2.COLOR_YCrCb2BGR)


# ============================================================================
# 4b. 局部自适应：暗部提清晰 + 糊区去软（两张掩膜，逐像素决定，无模型）
# ============================================================================

def _box(x, win):
    """局部均值（积分图实现，O(N)）。win 自动取奇数。"""
    win = max(3, win | 1)
    return cv2.boxFilter(x, -1, (win, win), normalize=True, borderType=cv2.BORDER_REFLECT)


def dark_detail_mask(Y, win=9, lo=0.10, hi=0.32, s_lo=0.010, s_hi=0.022):
    """暗部掩膜 = 「此处暗」×「此处有结构」。0..1 float。

    为什么不是"暗就够了"：
        暗部里的纯平坦区（黑背景、阴影空洞）没有东西可提，硬提只会把噪点
        和 AI 颗粒拉出来。必须叠一层局部标准差，证明这里有纹理才算数。

    ★ 结构门的窗口必须用 9，不能用 41（2026-10-01 修，实测证据）：
        原实现 win=41 + (0.015, 0.045)。41px 窗口里，一段**缓坡渐变**的
        窗口内跨度也有几个色阶 ⇒ sd 被撑到 0.0285，门开到 **45%**。
        后果：8 位灰渐变上被改了颜色（实测最大差 2），
        违反"平坦区零改动"这条底线。
        换 9px 后两个世界的距离立刻拉开：
            渐变暗段   sd(41)=0.0285  →  sd(9)=0.0071   门 0.452 → 0.000
            真实包面   sd(41)=0.0641  →  sd(9)=0.0255   门 0.612 → 0.257
        原因：缓坡在**小窗口内几乎是平的**，只有真纹理才有局部起伏。
        门限随之从 (0.015,0.045) 调到 (0.010,0.022) —— 配合小窗口的尺度。

    实测（示例人像图）：黑包 0.58 / 上衣 0.45 / 裤 0.28 / 脸 0.14 /
                        天空 0.002 / 橙墙 0.000
    门槛 lo/hi 标定依据：黑包局部亮度 0.128、上衣 0.169、裤 0.251、脸 0.375。
        取 lo=0.10 hi=0.32，让"脸"落在斜坡尾部（0.14）而不是被完全排除 ——
        脸部暗侧确实有可提的纹理，但不该像黑包那样吃满。

    换窗口后各区域门值实测（旧值 → 新值）：
        脸 0.111→0.037  耳环 0.017→0.004  黑衣 0.396→0.234
        黑包 0.472→0.267  黑裤 0.420→0.435（反而升）  蓝天 0.000→0.000
        灰渐变 0.081→0.004（这才是重点：误判被关掉了）
    ⇒ 真实暗部该拿的照拿（黑裤甚至更多），渐变不再被误伤。
    """
    m = _box(Y, win)
    sd = np.sqrt(np.maximum(_box(Y * Y, win) - m * m, 0.0))
    dark = 1.0 - _smoothstep(m, lo, hi)
    struct = _smoothstep(sd, s_lo, s_hi)
    return np.clip(dark * struct, 0.0, 1.0)


def _downup(y, f=2):
    """降采样再升采样 —— 用来问"这张图的细节扛不扛得住重采样"。"""
    h, w = y.shape[:2]
    return cv2.resize(
        cv2.resize(y, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA),
        (w, h), interpolation=cv2.INTER_LANCZOS4)


def blur_detail_mask(Y, win=9, mid_lo=0.008, mid_hi=0.025):
    """糊区掩膜 = 「细节扛不住重采样」×「有中频内容」。0..1 float。

    ★ 判据（这一步换过三次，记下来免得再绕回去）：
      第 1 版用「高频/中频的比值」。问题：**基准取谁**都得靠图自身统计，
        整图均匀发虚时，基准被一起拉低，反而看不出糊（实测整图模糊 2.5
        的图被判成"清晰"）。这是根本缺陷，不是调参能救的。
      第 2 版用「高频绝对值」。不行：暗部纹理天然高频低，会把清晰的暗部误判成糊。
      第 3 版（当前）用「**降采样再升采样后的高频损失**」——
        问的是"把图缩一半再放大回来，高频丢了多少"。
        清晰图丢一大截（0.17~0.41），本来就糊的图几乎不丢（0.05~0.08）。
        这个量**与内容多少无关**，只与"是否已经糊了"有关，所以整图均匀发虚也认得出来。

    ★ 结构门用**中频**而不是高频：
        高频正是被衡量、被模糊削弱的那个量，拿它当门会自相矛盾
        （实测：模糊越重，门越为 0，掩膜反而越小 —— 方向是反的）。
        中频（sigma=4 的带通）对轻度模糊不敏感，只有当整块真的糊平了才归零。

    标定实测（细节损失中位数，越大越清晰）：
        人像   清晰 0.171 → 模糊0.8 0.114 → 1.5 0.076 → 2.5 0.055
        商品图 清晰 0.407 → 0.146 → 0.047 → 0.015
        文字图 清晰 0.229 → 0.119 → 0.049 → 0.023
        ⇒ 三档单调，不同内容类型都成立，阈值 0.16 能分开。
    """
    a = _box(np.abs(cv2.Laplacian(Y, cv2.CV_32F, ksize=3)), win)
    b = _box(np.abs(cv2.Laplacian(_downup(Y), cv2.CV_32F, ksize=3)), win)
    loss = np.clip((a - b) / (a + 1e-5), 0.0, 1.0)
    # 结构门：用大窗口局部标准差（与频率无关，糊得再重也还在），
    # 避免"越糊→门越小→掩膜反而越弱"这种自相矛盾。
    mean = _box(Y, 121)
    struct = _smoothstep(np.sqrt(np.maximum(_box(Y * Y, 121) - mean * mean, 0.0)),
                         0.015, 0.050)            # 没内容的区域不叫"糊"，叫"平"
    return np.clip((1.0 - _smoothstep(loss, 0.06, 0.16)) * struct,
                   0.0, 1.0)


def clarity_boost(bgr, gain=1.0, r_small=4, r_big=32, eps=0.0025,
                  e_lo=0.030, e_hi=0.160, win=9, sd_lo=0.008, sd_hi=0.022):
    """局部对比增强（Clarity）—— 放大「中等尺度的明暗过渡」，不是锐化。

    ★ 为什么需要它（2026-10-01 实测，用户第三次说"跟原图差不多"）：
        量下来，锐化把 HF（像素级锐度）提了 +46%，看着数字很大，
        但**局部对比只提了 +5.4%** —— 人眼判断"清不清楚"根本不看那个数。
        把图按频段拆开看能量占比：
            极高频 (sigma 0.7)   0.7%   ← 锐化主要动这一段
            高频   (sigma 2.0)   6.9%
            中频   (sigma 8.0)  34.8%   ← 人眼主要看这段
            低频   (sigma 24)   57.6%
        ⇒ "HF +46% 但看不出差别"不是错觉，是量在了错的地方。

    ★ 为什么不能简单把中频放大（试过，数据在这里）：
        高斯中频 gain 0.5 ⇒ 局部对比 +40%，但光晕像素 15.0%、平坦区最大差 16
        高斯中频 gain 0.7 ⇒ 局部对比 +59%，但光晕像素 28.6%、平坦区最大差 21
        强边两侧必然过冲 —— 石墙与包的交界会出白边。这不是调参能绕开的。

    ★ 本函数用的三条对策：
        ① 保边分解：用导引滤波（小半径 / 大半径之差）代替高斯。
           两边都保边，所以强边不会被"过度矫正"。
        ② 边缘门控：按平滑后的梯度幅值给增益 ——
           强边（>= e_hi）处增益归零，中等纹理处满增益。
           光晕只出在强边两侧，那里不增益就没有光晕。
        ③ 平坦门：局部标准差太小的地方（白底/渐变）严格不动。
           ★ 窗口必须用 9 而不是 61（实测踩过，2026-10-01）：
             用 61px 大窗口算标准差时，**缓坡渐变和真实纹理分不开** ——
                 灰渐变 sd(61)=0.0472   真实包面 sd(61)=0.0697   ← 只差 1.5 倍
             门限 0.020 被渐变轻松超过，于是灰渐变被当成"有内容"，
             色阶被改了 7 个 —— 违反"平坦区零改动"这条底线。
             换成 9px 小窗口立刻分开：
                 灰渐变 sd(9)=0.0073    真实包面 sd(9)=0.0234   ← 差 3.2 倍
             原因：缓坡在**小窗口内几乎是平的**（相邻像素差极小），
             只有真实纹理才有局部起伏。小窗口量的才是"纹理度"。
             改后：灰渐变最大差 7 -> 0（回到零改动），
             而真实包面的中频增益不变（数值见下方实测）。

    ★ 实测（示例包图，叠加在「全面清晰」之上）：
        gain 1.0：局部对比 +7.2%  光晕>3 占 2.36%  光晕>6 占 0.24%
                  HF +41.5%  平坦区最大差 8（可接受）
        gain 1.5：局部对比 +8.7%  光晕 5.5%   ← 开始看得见
        gain 2.0：局部对比 +9.3%  光晕 10.1%  ← 明显白边，不取
        ⇒ 预设定在 gain 1.0。再往上就不是"增强"而是"失真"了。
    """
    if gain <= 0:
        return bgr
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    Y = ycc[..., 0] / 255.0
    lc = guided_filter(Y, Y, r_small, eps) - guided_filter(Y, Y, r_big, eps)
    gx = cv2.Sobel(Y, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(Y, cv2.CV_32F, 0, 1, ksize=3)
    edge = _box(np.sqrt(gx * gx + gy * gy), 9)
    gate = 1.0 - _smoothstep(edge, e_lo, e_hi)
    m = _box(Y, win)
    sd = np.sqrt(np.maximum(_box(Y * Y, win) - m * m, 0.0))
    gate = gate * _smoothstep(sd, sd_lo, sd_hi)
    Y2 = np.clip(Y + lc * gain * gate, 0.0, 1.0)
    ycc[..., 0] = Y2 * 255.0
    return cv2.cvtColor(ycc.astype(np.uint8), cv2.COLOR_YCrCb2BGR)


def adaptive_sharpen(bgr, amount=2.0, radius=3, eps=0.0025, soft=0.010,
                     floor=0.10, t0n=0.075, t1n=0.34, tol=0.004,
                     dark_gain=0.0, blur_gain=0.0, cap=1.6,
                     relative=None, return_maps=False, protect=None):
    """局部自适应锐化：暗部与糊区各自拿加成，平坦区严格 0。

    ★ 相对梯度（mag / (局部亮度 + floor)）是让暗部"进得来"的关键。
      绝对门限 t0=0.045 对亮部合适，但暗部纹理的梯度天然就小（暗处对比度低），
      全被挡在门外 —— 这就是黑包区只有 +7% 的原因。
      换成相对门限后，暗部同等"相对对比"的纹理也能进门限，
      而亮部行为基本不变（floor=0.10、t0n=0.075 ≈ 中等亮度下的绝对 0.045）。

    dark_gain / blur_gain  两张掩膜对强度的加成倍率，0 = 关掉该模块
    cap                    加成上限，防止暗部被过度锐化出光晕
    relative               相对梯度开关。None = 自动（有暗部加成时用相对、否则用绝对）。
                           ★ 为什么要这个开关：相对门限会让暗部整体进门限，
                             如果只想"救糊"却顺带把暗部也提了，两个预设就分不开了。
                             分开之后：暗部档才用相对门限，救糊档保持绝对门限。
    protect                0..1 的"基础锐化保护"掩膜（物件保护）。
                           ★ 它只削**基础锐化**这一项，不削暗部/糊区加成。
                             为什么：物件保护的初衷是"别把包当脸一起锐"，
                             但一开始连加成一起削了 —— 实测「暗部更清晰」档
                             在包/裤上 78%~83% 的加成被吃掉，预设等于白给。
                             改成分开削之后：包照原样被保护（不爬纹），
                             但"这里暗且有纹理"这个判据该拿的清晰度照拿。
    """
    if amount <= 0:
        return (bgr, None, None, None) if return_maps else bgr
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    Y = ycc[..., 0] / 255.0
    base = guided_filter(Y, Y, radius, eps)
    detail = Y - base
    if soft > 0:
        a = np.abs(detail)
        detail = detail * _smoothstep(a, soft, 2.0 * soft)

    gx = cv2.Sobel(Y, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(Y, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    if relative is None:
        relative = dark_gain > 0
    if relative:
        w = _smoothstep(mag / (_box(Y, 25) + floor), t0n, t1n)
    else:
        # 绝对门限：与历史交付物完全一致的那条路径
        w = _smoothstep(mag, 0.045, 0.20)

    dmap = dark_detail_mask(Y) if dark_gain > 0 else np.zeros_like(Y)
    bmap = blur_detail_mask(Y) if blur_gain > 0 else np.zeros_like(Y)
    base = amount if protect is None else amount * (1.0 - np.clip(protect, 0.0, 1.0))
    amap = base + amount * (min(dark_gain, cap) * dmap + min(blur_gain, cap) * bmap)
    Y2 = Y + detail * amap * w
    if tol > 0:
        k = np.ones((3, 3), np.float32)
        Y2 = np.clip(Y2, cv2.erode(Y, k) - tol, cv2.dilate(Y, k) + tol)
    ycc[..., 0] = np.clip(Y2, 0.0, 1.0) * 255.0
    out = cv2.cvtColor(ycc.astype(np.uint8), cv2.COLOR_YCrCb2BGR)
    if return_maps:
        return out, dmap, bmap, amap * w
    return out


# ============================================================================
# 4c. 预设：把"符合预期"的几套参数固化下来
# ============================================================================

PRESETS = {
    "推荐": {
        "desc": "人物区锐化，暗且中性的物件区保持原图。最稳，任何图都不会失真。",
        "amount": 2.0, "dark_gain": 0.0, "blur_gain": 0.0,
        "obj_safe": True, "scale": 1.0,
    },
    "暗部更清晰": {
        "desc": "在推荐档基础上，给暗部（黑包/黑衣/阴影）加局部锐化。只提有纹理的暗部，平坦黑背景不动。",
        "amount": 2.0, "dark_gain": 0.9, "blur_gain": 0.0,
        "obj_safe": False, "scale": 1.0,
    },
    "救糊图": {
        "desc": "给发虚发软的区域补锐度。判据是高频/中频能量比，逐图自归一化，清晰的地方不碰。",
        "amount": 2.0, "dark_gain": 0.0, "blur_gain": 1.2,
        "obj_safe": True, "scale": 1.0,
    },
    "全面清晰": {
        "desc": "暗部 + 糊区同时开。力度最大，适合原图偏软又想一次到位。",
        "amount": 2.2, "dark_gain": 0.9, "blur_gain": 1.2,
        "obj_safe": False, "scale": 1.0,
    },
    "轻微": {
        "desc": "已经很干净、只想补一点点锐度的图。平坦区照样零改动。",
        "amount": 1.2, "dark_gain": 0.0, "blur_gain": 0.0,
        "obj_safe": True, "scale": 1.0,
    },
    "对比感增强": {
        "desc": "锐化更强 + 额外拉开中等尺度的明暗过渡（褶皱/结构），效果最明显的一档。"
                "代价：不再是零失真，强边处有极轻微光晕（实测 0.4% 像素，最大过冲 9/255）。",
        "amount": 3.5, "dark_gain": 0.9, "blur_gain": 1.2, "clarity": 1.0,
        "obj_safe": False, "scale": 1.0,
    },
    "清晰 + 放大2倍": {
        "desc": "在全面清晰的基础上做 2 倍 Lanczos 放大（插值，不是超分，不会增加细节）。",
        "amount": 2.2, "dark_gain": 0.9, "blur_gain": 1.2,
        "obj_safe": False, "scale": 2.0,
    },
}


def apply_preset(name):
    """取预设；名字不认识时回退到推荐档。

    clarity 只有「对比感增强」那档有 —— 不补默认值的话调用方每次都得写
    p.get("clarity", 0.0)，漏一处就是 KeyError。这里统一补齐。
    """
    p = dict(PRESETS.get(name, PRESETS["推荐"]))
    p.setdefault("clarity", 0.0)
    return p


# ============================================================================
# 4. 值不值得跑：三个可计算的信号，无模型、无人工、无 LLM
# ============================================================================

BAND_RUN = 0.070     # 门限带内像素占比 >= 此值 ⇒ 有东西可锐
BAND_SKIP = 0.055    # <= 此值 ⇒ 基本是平坦白底
MASK_RUN = 0.06      # 物件遮罩覆盖 >= 此值 ⇒ 有主体可分区
MASK_SKIP = 0.015
GAIN_RUN = 0.08      # 链路存活增益 >= 8% ⇒ 肉眼可辨
GAIN_SKIP = 0.035    # <= 3.5% ⇒ 噪声级


def band_ratio(bgr, t0=0.045, t1=0.20):
    """Sobel 幅度落在锐化门限带 [t0, t1) 的像素占比。

    这才是真正拿到增益的像素：低于 t0 的平坦区增益严格为 0，
    高于 t1 的强边即使不锐化也已经够硬。
    实测：平坦白底 0.036~0.055；带材质/褶皱/链条的图 0.12~0.46。
    """
    y = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)[..., 0] / 255.0
    gx = cv2.Sobel(y, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(y, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    return float(((mag >= t0) & (mag < t1)).mean())


def chain_hf(src_bgr, out_bgr, width=750, quality=85):
    """按平台真实链路（缩放 -> JPEG -> 解码）测增益还能剩多少。

    全尺寸 +25% 但过完链路只剩 +2%，等于白干 —— 这个函数就是用来戳破那种幻觉的。
    """
    def through(bgr):
        x = bgr
        if width and width < bgr.shape[1]:
            h = int(round(bgr.shape[0] * width / bgr.shape[1]))
            x = cv2.resize(x, (width, h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", x, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else x
    a, b = through(src_bgr), through(out_bgr)
    ha, hb = _hf(a), _hf(b)
    return ha, hb, ((hb - ha) / ha if ha > 1e-6 else 0.0)


def measure(path, width=750, amount=2.0, cap=2048):
    """测一张图，返回判决字典。只读，不改任何图。"""
    src = _read(path)
    long_side = max(src.shape[:2])
    if long_side > cap:
        k = cap / long_side
        src = cv2.resize(src, (int(round(src.shape[1] * k)), int(round(src.shape[0] * k))),
                         interpolation=cv2.INTER_AREA)
    out = sharpen_edges(src, amount)
    cov = float(object_mask(src).mean())
    br = band_ratio(src)
    hf0, hf1 = _hf(src), _hf(out)
    ch_a, ch_b, ch_gain = chain_hf(src, out, width=width)

    votes_run = sum([br >= BAND_RUN, cov >= MASK_RUN, ch_gain >= GAIN_RUN])
    votes_skip = sum([br <= BAND_SKIP, cov <= MASK_SKIP, ch_gain <= GAIN_SKIP])
    if votes_run >= 2 and ch_gain > GAIN_SKIP:
        verdict, why = "run", "有中梯度细节且增益能活到平台尺寸"
    elif votes_skip >= 2:
        verdict, why = "skip", "平坦白底/无可识别主体，链路存活增益在噪声级"
    else:
        verdict, why = "marginal", "介于两者之间"
    return {
        "file": path, "size": "%dx%d" % (src.shape[1], src.shape[0]),
        "hf_full": round(hf0, 2), "hf_out": round(hf1, 2),
        "hf_gain_full": round((hf1 - hf0) / hf0, 4) if hf0 > 1e-6 else 0.0,
        "band_ratio": round(br, 4), "mask_cov": round(cov, 4),
        "chain_width": width, "hf_chain_src": round(ch_a, 2),
        "hf_chain_out": round(ch_b, 2), "hf_gain_chain": round(ch_gain, 4),
        "verdict": verdict, "why": why,
    }


# ============================================================================
# 5. 处理一张图
# ============================================================================

def enhance_one(bgr, amount=2.0, obj_mode="original",
                dark_gain=0.0, blur_gain=0.0, cap=1.6, clarity=0.0,
                display_w=0):
    """返回 (成品, 遮罩)。

    display_w —— 「这张图最终会以多宽展示」。给了它且原图比它宽时，
        所有**尺度相关**的参数（锐化半径、过冲容差、梯度门限、clarity 的窗）
        会按 display_w/原图宽 的比例放大，让处理在**显示尺度**上生效，
        而像素尺寸保持原样、一个像素都不缩。
        ★ 它是"不缩图也保得住效果"的那条路：平台把 1200 缩到 750 展示时，
          在 1200 上锐化出来的高频大半被重采样吃掉（实测只剩一半），
          把半径同步放大到显示尺度，缩完剩下的才等于"直接在 750 上锐化"。
        16 张真实商品图实测（都换算到 750 显示尺寸量）：
            不缩放半径（旧）      HF +20.6%   局部对比 +3.9%
            缩到 750 再锐化       HF +32.5%   局部对比 +5.2%
            不缩像素 + 半径归一   HF +27.7%   局部对比 +6.9%  ← 局部对比最高

    默认档（obj_mode="original"）：
        人物/主体区 = 门限保边锐化；暗且中性的物件区 = 原图像素，一个字节都不动。
        对应「人脸要那个处理法、包别压黑」那条决定。
    obj_mode="enhance" 是旧档（物件也锐化 + 压黑），实测会把暗部细节一起沉下去。

    dark_gain / blur_gain > 0 时走 adaptive_sharpen（相对梯度门限），
    暗部与糊区各自拿加成；两者都为 0 时走原始档，行为与历史交付物逐像素一致。

    ★ 物件保护只挡"基础锐化"，不挡暗部/糊区加成（2026-10-01 修）：
        修之前是"先自适应锐化、再和原图按遮罩混合"，等于把加成也一起按遮罩削掉。
        实测「暗部更清晰」档黑包/黑裤的加成被吃掉 78%~83%，预设形同虚设 ——
        预设名字写着"黑包更清楚"，拿到的却是接近推荐档的数字。
        修法：把物件遮罩作为 protect 传进 adaptive_sharpen，
        让它只削基础项，加成项照拿。
        改后（示例图，HF 变化）：
            预设            黑衣    黑包    黑裤    脸
            推荐           +19%    +9%     +6%   +40%
            暗部更清晰      +29%   +29%    +28%   +50%
            全面清晰        +31%   +32%    +29%   +57%
        「推荐/轻微」两档 dark_gain=blur_gain=0，走的还是老路径，
        逐像素与历史交付物一致（回归已验证）。
    """
    m = object_mask(bgr)
    adaptive = dark_gain > 0 or blur_gain > 0
    # ★ 尺度归一：把"3 像素半径"这类固定尺度换算到显示尺度上。
    #   只放大不缩小（s >= 1）—— 原图比显示宽度还小时没必要动，
    #   那种情况平台不会缩，处理尺度本来就是对的。
    s = 1.0
    if display_w and display_w > 0:
        w = bgr.shape[1]
        if w > display_w:
            s = float(w) / float(display_w)
    if adaptive and obj_mode == "original":
        # 物件区不参与基础锐化，但暗部/糊区该拿的加成照拿
        out = adaptive_sharpen(bgr, amount, max(1, int(round(3 * s))), 0.0025, soft=0.0,
                               dark_gain=dark_gain, blur_gain=blur_gain,
                               cap=cap, protect=m,
                               tol=0.004 * s, t0n=0.075 / s, t1n=0.34 / s)
        if clarity > 0:
            out = clarity_boost(out, clarity, r_small=max(2, int(round(4 * s))),
                                r_big=max(8, int(round(32 * s))),
                                win=max(3, int(round(9 * s)) | 1))
        return out, m
    if adaptive:
        sharp = adaptive_sharpen(bgr, amount, max(1, int(round(3 * s))), 0.0025, soft=0.0,
                                 dark_gain=dark_gain, blur_gain=blur_gain, cap=cap,
                                 tol=0.004 * s, t0n=0.075 / s, t1n=0.34 / s)
    else:
        sharp = sharpen_edges(bgr, amount, max(1, int(round(3 * s))), 0.0025,
                              0.045 / s, 0.20 / s, 0.004 * s)
    if obj_mode == "original":
        obj = bgr
    else:
        obj = sharpen_edges(bgr, amount * 2.0, 3, 0.0025, 0.0315, 0.20, 0.004, 0.010)
    out = np.clip(sharp.astype(np.float32) * (1 - m[..., None])
                  + obj.astype(np.float32) * m[..., None], 0, 255).astype(np.uint8)
    return out, m


def upscale(bgr, scale):
    """纯 Lanczos 放大（无模型）。放在锐化之后，避免把噪点一起放大。"""
    if scale <= 1.0:
        return bgr
    return cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)


def fit_long_edge(bgr, target):
    """把长边缩到 target（只缩不放）。返回 (图, 实际缩放比)。

    ★ 为什么缩图要发生在**锐化之前**（2026-10-01 实测，这是本工具最反直觉的一条）：
        平台展示时会把图缩到自己的显示宽度（常见 750）。如果先在 1200 锐化、
        平台再缩到 750，锐化出来的那层高频**大部分被重采样吃掉了**：
            在 1200 锐化 → 平台缩到 750：  HF 只有 +15.5%
        反过来，先缩到 750 再在那上面锐化：
            缩 750 再锐化 → 直接上传：      HF +24.2%
        同一个档位、同一张图，**后者不但文件小 4.7 倍，效果还强 1.6 倍**。
        根因：锐化的"半径"是固定的 3 像素，缩放会把这个物理尺度改掉；
        先缩后锐，锐出来的频率正好落在最终显示的那张图上。
        四张真实商品图全量实测（HF 增益）：
            档位          1200锐化后缩     缩750再锐化
            全面清晰       10~31%          16~41%
            对比感增强      13~39%          17~56%
    """
    if not target or target <= 0:
        return bgr, 1.0
    h, w = bgr.shape[:2]
    m = max(h, w)
    if m <= target:
        return bgr, 1.0
    s = float(target) / float(m)
    out = cv2.resize(bgr, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                     interpolation=cv2.INTER_AREA)
    return out, s


def report(src, out, mask):
    """按亮度档给读数 —— 比固定区域通用，任何图都能看。"""
    y0 = cv2.cvtColor(src, cv2.COLOR_BGR2YCrCb).astype(np.float32)[..., 0]
    y1 = cv2.cvtColor(out, cv2.COLOR_BGR2YCrCb).astype(np.float32)[..., 0]
    g0 = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hp0 = np.abs(cv2.Laplacian(g0, cv2.CV_32F, ksize=3))
    hp1 = np.abs(cv2.Laplacian(g1, cv2.CV_32F, ksize=3))
    print()
    print("%-16s %9s %9s %9s | %8s %8s %8s | %8s" %
          ("亮度档", "Y均", "dY", "dY%", "HF", "HF后", "HF变化", "遮罩"))
    bands = [("暗部 (<64)", y0 < 64), ("中间调 (64-160)", (y0 >= 64) & (y0 < 160)),
             ("亮部 (>=160)", y0 >= 160)]
    for name, sel in bands:
        if int(sel.sum()) < 64:
            print("%-16s %9s" % (name, "(像素太少，跳过)"))
            continue
        a0, a1 = y0[sel], y1[sel]
        d = float(a1.mean() - a0.mean())
        h0, h1 = hp0[sel].mean(), hp1[sel].mean()
        print("%-16s %9.1f %+9.1f %+8.1f%% | %8.2f %8.2f %+7.0f%% | %8.3f"
              % (name, a0.mean(), d, 100 * d / max(a0.mean(), 1e-9),
                 h0, h1, 100 * (h1 / max(h0, 1e-9) - 1), mask[sel].mean()))
    print("整图 HF %.2f -> %.2f (%+.1f%%)   遮罩覆盖 %.1f%%"
          % (_hf(src), _hf(out), 100 * (_hf(out) / max(_hf(src), 1e-9) - 1),
             100 * mask.mean()))


# ============================================================================
# 6. 自检：不依赖任何外部图，验证的是「不许发生的事」
# ============================================================================

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def selftest():
    print("自检 —— 验证的是「不许发生的事」，不是「结果好不好看」\n")
    fails = []

    def check(name, cond, detail=""):
        print(("  [OK] " if cond else "  [XX] ") + name + ("   " + detail if detail else ""))
        if not cond:
            fails.append(name)

    # 1) 纯色平坦图：一个字节都不许动
    flat = np.full((256, 256, 3), 128, np.uint8)
    o = sharpen_edges(flat, 2.0)
    check("纯色平坦图：锐化后零改动", np.array_equal(flat, o),
          "最大差 %d" % int(np.abs(flat.astype(int) - o.astype(int)).max()))

    # 2) 平滑渐变也零改动（爬纹试纸）
    xs = np.linspace(40, 200, 256, dtype=np.float32)
    grad = np.repeat(xs[None, :], 256, axis=0)
    grad = np.dstack([grad, grad, grad]).astype(np.uint8)
    o = sharpen_edges(grad, 2.0)
    d = int(np.abs(grad.astype(int) - o.astype(int)).max())
    check("平滑渐变（无边缘）：零改动", d <= 1, "最大差 %d（允许 1 的舍入）" % d)

    # 3) 有边缘的图必须真的变锐
    edge = np.full((256, 256, 3), 60, np.uint8)
    edge[:, 128:] = 200
    edge[64:96, 32:96] = 220
    o = sharpen_edges(edge, 2.0)
    check("有边缘的图：确实变锐", _hf(o) > _hf(edge) * 1.02,
          "HF %.2f -> %.2f" % (_hf(edge), _hf(o)))

    # 4) 极小图不能崩（曾在 <24px 的图上 reshape 崩过）
    ok = True
    try:
        for n in (8, 16, 23, 24, 25, 40):
            tiny = np.random.RandomState(n).randint(0, 255, (n, n, 3), np.uint8)
            object_mask(tiny)
            enhance_one(tiny)
    except Exception as e:                       # noqa: BLE001
        ok = False
        print("       ", e)
    check("极小图（8/16/23/24/25/40 px）不崩", ok)

    # 5) 遮罩形状与值域
    rnd = np.random.RandomState(0).randint(0, 255, (200, 300, 3), np.uint8)
    m = object_mask(rnd)
    check("遮罩形状正确且值域在 [0,1]",
          m.shape == rnd.shape[:2] and 0.0 <= m.min() and m.max() <= 1.0,
          "shape %s  min %.3f max %.3f" % (m.shape, float(m.min()), float(m.max())))

    # 6) amount=0 必须恒等
    photo = np.zeros((128, 128, 3), np.uint8)
    cv2.circle(photo, (64, 64), 40, (180, 140, 90), -1)
    cv2.rectangle(photo, (20, 20), (60, 45), (40, 40, 40), -1)
    check("amount=0 时恒等", np.array_equal(sharpen_edges(photo, 0.0), photo))

    # 7) 中文路径往返
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_自检临时.png")
    try:
        _write(tmp, photo)
        ok = np.array_equal(photo, _read(tmp))
        os.remove(tmp)
    except Exception as e:                       # noqa: BLE001
        ok = False
        print("       ", e)
    check("中文路径读写往返一致", ok)

    # 8) 判定器给出三档之一，且可重复
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_自检临时2.png")
    try:
        _write(tmp, photo)
        v1, v2 = measure(tmp), measure(tmp)
        ok = v1["verdict"] in ("run", "skip", "marginal") and v1 == v2
    except Exception as e:                       # noqa: BLE001
        ok = False
        print("       ", e)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    check("判定器输出三档之一且可重复", ok)

    # 9) 亮度不许漂移（颜色和明暗不是这个工具该碰的）
    y0 = float(cv2.cvtColor(photo, cv2.COLOR_BGR2YCrCb)[..., 0].astype(float).mean())
    y1 = float(cv2.cvtColor(sharpen_edges(photo, 2.0),
                            cv2.COLOR_BGR2YCrCb)[..., 0].astype(float).mean())
    drift = abs(y1 - y0) / y0
    check("整体亮度漂移 < 1%", drift < 0.01, "漂移 %.3f%%" % (drift * 100))

    print()
    if fails:
        print("有 %d 项未通过：" % len(fails))
        for f in fails:
            print("   -", f)
        return 1
    print("全部通过（9 项）。")
    return 0


# ============================================================================
# 7. 命令行
# ============================================================================

def iter_images(root):
    """扫目录里的图。

    ★ 必须跳过的三类（2026-10-01 实测踩过）：
        场景：源图在 grid\\，输出目录设成 grid\\xxx.png_增强（在源目录**里面**）。
        第一次跑完，_增强 里就有 4 张 _enh.png；第二次再对 grid\\ 跑，
        os.walk 会把那 4 张当成新输入，产出 _enh_enh.png，越滚越多 ——
        这就是「源和工具互相污染」。
        实测：4 张源图跑出 6 张成品，多出来的两张是 grid-1-1-top-left_enh_enh.png。

        所以：① 名字以 _增强 结尾的目录整个跳过（本工具的默认输出目录名）；
              ② 以 _enh.png 结尾的文件跳过（本工具的成品命名）；
              ③ chk 类临时文件（.prep.）跳过（那是看图前的压缩副本）。
        三者都是本工具自己的产物，不该再被当输入。
    """
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.endswith("_增强")]
        for f in sorted(files):
            low = f.lower()
            if not low.endswith(EXTS):
                continue
            if ".prep." in low:            # 压缩副本
                continue
            if low.endswith("_enh.png") or low.endswith("_enh.jpg") or low.endswith("_enh.jpeg"):
                continue                    # 本工具自己的成品
            yield os.path.join(dirpath, f)


def run_batch(indir, outdir, amount, judge_only, force, scale,
              dark_gain=0.0, blur_gain=0.0, clarity=0.0,
              shrink=0, fmt="png", quality=92, display_w=0, compress_only=False):
    files = list(iter_images(indir))
    if not files:
        print("[X] 目录里没找到图: " + indir)
        return 1
    rows, t_all = [], time.time()
    n_run = n_skip = n_err = n_cmp = 0
    # ★ 三档格式：png 无损 / jpeg 默认(q92) / jpeg-hi 高保真(q95 + 4:4:4 + optimize)
    is_jpg = fmt in ("jpeg", "jpeg-hi")
    ext = ".jpg" if is_jpg else ".png"
    hi = fmt == "jpeg-hi"
    q = (95 if hi else quality) if is_jpg else None
    tot_in = tot_out = 0
    for i, p in enumerate(files, 1):
        rel = os.path.relpath(p, indir)
        try:
            v = measure(p, amount=amount)
        except Exception as exc:                 # noqa: BLE001
            print("[%d/%d] 错 %s: %s" % (i, len(files), rel, str(exc)[:70]))
            rows.append({"file": rel, "verdict": "error", "why": str(exc)[:100]})
            n_err += 1
            continue
        # ★ compress_only：一个字都不改，只重新编码（+ 可选的缩图）。
        #   它是"增强到底有没有用"的对照组 —— 尤其是风格化的图，
        #   人物/物件本身已经足够柔和，光看压缩后的观感很难分辨，
        #   所以需要一条"原图直接压缩"的基准线摆在一起比。
        if compress_only:
            do, tag = False, "只压缩"
        else:
            do = force or v["verdict"] in ("run", "marginal")
            tag = "增强" if do else "直出"
        print("[%d/%d] %-8s %s  带内%.3f 链路%+.1f%%  %s"
              % (i, len(files), v["verdict"], tag, v["band_ratio"],
                 v["hf_gain_chain"] * 100, rel))
        if not judge_only:
            out_path = os.path.join(outdir, os.path.dirname(rel),
                                    os.path.splitext(os.path.basename(rel))[0]
                                    + ("_cmp" if compress_only else "_enh") + ext)
            if compress_only:
                # 不锐化、不归一尺度 —— 只按需缩图 + 按 format/quality 重新编码
                work, _s = fit_long_edge(_read(p), shrink)
                _write(out_path, upscale(work, scale), q, hi)
            elif do:
                src = _read(p)
                # ★ 顺序要紧：先按长边缩图，再锐化 —— 缩图在锐化之后会把锐出来的
                #   高频采掉（实测剩下的不到一半），见 fit_long_edge 的注释。
                work, _s = fit_long_edge(src, shrink)
                out, _m = enhance_one(work, amount, dark_gain=dark_gain,
                                      blur_gain=blur_gain, clarity=clarity,
                                      display_w=display_w)
                _write(out_path, upscale(out, scale), q, hi)
            elif os.path.splitext(p)[1].lower() == ".png" and scale <= 1.0:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                shutil.copy2(p, out_path)        # 跳过 = 逐字节原样，连重编码都不做
            else:
                work, _s = fit_long_edge(_read(p), shrink)
                _write(out_path, upscale(work, scale), q, hi)
            try:
                tot_in += os.path.getsize(p)
                tot_out += os.path.getsize(out_path)
            except OSError:
                pass
        if compress_only:
            n_cmp += 1
        else:
            n_run += 1 if do else 0
            n_skip += 0 if do else 1
        rows.append({"file": rel, "verdict": v["verdict"], "action": tag,
                     "band_ratio": v["band_ratio"], "mask_cov": v["mask_cov"],
                     "hf_gain_chain": v["hf_gain_chain"]})

    rep = os.path.join(outdir, "_处理报告.csv")
    if not judge_only:
        os.makedirs(outdir, exist_ok=True)
        keys = ["file", "verdict", "action", "band_ratio", "mask_cov", "hf_gain_chain"]
        with open(rep, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    dt = time.time() - t_all
    if compress_only:
        print("\n共 %d 张：只压缩 %d / 出错 %d     耗时 %.1fs（%.2fs/张）"
              % (len(files), n_cmp, n_err, dt, dt / max(1, len(files))))
        print("★ 这一轮没有做任何增强（像素未改），只是重新编码 —— 用于对照")
    else:
        print("\n共 %d 张：增强 %d / 直出 %d / 出错 %d     耗时 %.1fs（%.2fs/张）"
              % (len(files), n_run, n_skip, n_err, dt, dt / max(1, len(files))))
    if not judge_only and tot_in:
        print("体积：%.1f MB -> %.1f MB（省 %.0f%%）"
              % (tot_in / 1048576.0, tot_out / 1048576.0,
                 (1 - tot_out / float(tot_in)) * 100))
    if not judge_only:
        print("报告 ->", rep)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="图片增强：门限保边锐化 + 自动分流（纯 CPU，无模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：\n"
               "  python enhance.py -i 图.jpg -o out\\\n"
               "  python enhance.py -i 图目录 -o out\\\n"
               "  python enhance.py -i 图.jpg --judge\n"
               "  python enhance.py --selftest")
    ap.add_argument("-i", "--input", help="图片或目录")
    ap.add_argument("-o", "--output", help="输出目录")
    ap.add_argument("--amount", type=float, default=2.0, help="锐化强度（默认 2.0）")
    ap.add_argument("--preset", default=None,
                    help="参数预设：" + " / ".join(PRESETS.keys()))
    ap.add_argument("--list-presets", action="store_true", help="列出所有预设")
    ap.add_argument("--scale", type=float, default=1.0, help="放大倍数（默认 1.0，纯 Lanczos）")
    ap.add_argument("--shrink", type=int, default=0, metavar="N",
                    help="先把长边缩到 N 像素再锐化（推荐 750 或 900）。"
                         "★ 缩图必须在锐化之前 —— 顺序反了效果会被平台缩放吃掉一半")
    ap.add_argument("--display-width", type=int, default=0, metavar="N",
                    help="不缩像素时的等效显示宽度（如 750）。给了它就不缩图，"
                         "改成把所有尺度参数换算到该宽度 —— 效果与「缩到 N 再锐化」相当，"
                         "但保留原始像素。与 --shrink 二选一")
    ap.add_argument("--format", choices=("png", "jpeg", "jpeg-hi"), default="png",
                    help="输出格式（默认 png 无损）。jpeg 体积小得多，肉眼几乎无差；"
                         "jpeg-hi = 高分保真 JPEG（q95 + 4:4:4 色度不下采样 + optimize），"
                         "给「像素一个都不少、只想把 MB 压成 KB」用，比 jpeg 大但色度更准")
    ap.add_argument("--quality", type=int, default=92,
                    help="jpeg 质量 1~100（默认 92，实测 PSNR 44dB）")
    ap.add_argument("--judge", action="store_true", help="只判定值不值得处理，不出图")
    ap.add_argument("--force", action="store_true", help="不做分流，一律处理")
    ap.add_argument("--obj-mode", choices=("original", "enhance"), default="original",
                    help="物件区：original=保持原图（默认，推荐）；enhance=也锐化（旧档，会沉暗部）")
    ap.add_argument("--report", action="store_true", help="打印各亮度档读数")
    ap.add_argument("--compress-only", action="store_true",
                    help="不做任何增强，只把原图重新编码（可配 --shrink/--format/--quality）。"
                         "用于对照：看看「只压缩」和「增强后压缩」到底差多少")
    ap.add_argument("--json", action="store_true", help="判定结果以 JSON 输出")
    ap.add_argument("--selftest", action="store_true", help="跑自检（不需要外部图）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.list_presets:
        print("可用预设：\n")
        for n, p in PRESETS.items():
            print("  %-14s amount=%.1f 暗部=%.1f 糊区=%.1f 放大=%.1fx"
                  % (n, p["amount"], p["dark_gain"], p["blur_gain"], p["scale"]))
            print("                 " + p["desc"] + "\n")
        return 0
    if not a.input:
        ap.error("要么 -i 给一张图/一个目录，要么 --selftest / --list-presets")

    # 预设优先：给了 --preset 就用预设里的值（显式 --amount 仍可覆盖金额）
    if a.preset:
        p = apply_preset(a.preset)
        amount, dark_gain, blur_gain, scale = p["amount"], p["dark_gain"], p["blur_gain"], p["scale"]
        clarity = p.get("clarity", 0.0)
        if a.amount != 2.0:
            amount = a.amount
        if a.scale != 1.0:
            scale = a.scale
        print("预设「%s」：%s" % (a.preset, p["desc"]))
    else:
        amount, dark_gain, blur_gain, scale, clarity = a.amount, 0.0, 0.0, a.scale, 0.0

    if os.path.isdir(a.input):
        outdir = a.output or (a.input.rstrip("\\/") + "_增强")
        return run_batch(a.input, outdir, amount, a.judge, a.force, scale,
                         dark_gain, blur_gain, clarity,
                         a.shrink, a.format, a.quality, a.display_width,
                         a.compress_only)
    if not os.path.isfile(a.input):
        print("[X] 找不到: " + a.input)
        return 1

    t0 = time.time()
    v = measure(a.input, amount=amount)
    if a.json:
        import json
        print(json.dumps(v, ensure_ascii=False, indent=2))
    else:
        print("判定 %s：带内 %.3f / 遮罩 %.3f / 链路增益 %+.1f%%  —— %s"
              % (v["verdict"].upper(), v["band_ratio"], v["mask_cov"],
                 v["hf_gain_chain"] * 100, v["why"]))
    if a.judge:
        return 0

    outdir = a.output or "out"
    is_jpg = a.format in ("jpeg", "jpeg-hi")
    hi = a.format == "jpeg-hi"
    ext = ".jpg" if is_jpg else ".png"
    base = os.path.splitext(os.path.basename(a.input))[0]
    # ★ 只压缩走 _cmp 后缀，增强走 _enh —— 两条路可以落在同一个目录里对比，
    #   互相不会覆盖（撞名了就没法等量对比了）。
    out_path = os.path.join(outdir, base + ("_cmp" if a.compress_only else "_enh") + ext)
    src = _read(a.input)
    q = (95 if hi else a.quality) if is_jpg else None
    if a.compress_only:
        # 只重新编码：不锐化、不归一尺度，像素级一个字节都不动（除非 --shrink/--scale）
        work, sfit = fit_long_edge(src, a.shrink)
        _write(out_path, upscale(work, scale), q, hi)
        print("只压缩（未做任何增强）-> %s  (%.2fs)" % (out_path, time.time() - t0))
        if sfit < 1.0:
            print("       长边 %d -> %d px" % (max(src.shape[:2]), max(work.shape[:2])))
        print("       体积 %.0f KB -> %.0f KB"
              % (os.path.getsize(a.input) / 1024.0, os.path.getsize(out_path) / 1024.0))
        return 0
    do = a.force or v["verdict"] in ("run", "marginal")
    if do:
        work, sfit = fit_long_edge(src, a.shrink)
        out, m = enhance_one(work, amount, a.obj_mode, dark_gain=dark_gain,
                             blur_gain=blur_gain, clarity=clarity,
                             display_w=a.display_width)
        _write(out_path, upscale(out, scale), q, hi)
        print("完成 -> %s  (%.2fs)" % (out_path, time.time() - t0))
        if sfit < 1.0:
            print("       长边 %d -> %d px（先缩后锐，效果优于先锐后缩）"
                  % (max(src.shape[:2]), max(out.shape[:2])))
        elif a.display_width and max(src.shape[:2]) > a.display_width:
            print("       像素保持 %d px，尺度按 %d px 显示宽度归一（不缩像素也保得住效果）"
                  % (max(src.shape[:2]), a.display_width))
        print("       体积 %.0f KB -> %.0f KB"
              % (os.path.getsize(a.input) / 1024.0, os.path.getsize(out_path) / 1024.0))
        if a.report:
            report(src, out, m)
    else:
        if (os.path.splitext(a.input)[1].lower() == ".png" and scale <= 1.0
                and a.format == "png" and not a.shrink):
            os.makedirs(outdir, exist_ok=True)
            shutil.copy2(a.input, out_path)
        else:
            work, _s = fit_long_edge(src, a.shrink)
            _write(out_path, upscale(work, scale), q, hi)
        print("判为跳过 —— 原图直出，未做任何改动 -> %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
