#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_sample.py —— 生成合成测试图（仓库里不放任何真实素材）。

为什么需要这个：
    仓库只放工具。真实源图是你的资产，不该进任何仓库 ——
    「源是源，工具是工具」，两者不相互污染。
    所以自测用的图一律现场合成：不依赖外部文件、可复现、可入库。

用法：
    python make_sample.py            # 生成到 _sample/
"""
import os
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_sample")


def fashion_portrait(size=1200):
    """模拟一张"带材质与场景的时装图"：亮背景 + 暗色主体 + 细纹理 + 硬边。"""
    rng = np.random.RandomState(20261001)
    h = w = size
    img = np.zeros((h, w, 3), np.uint8)
    # 天空渐变 + 暖色墙
    for y in range(h):
        img[y, :, :] = (200 - y * 60 // h, 140 + y * 40 // h, 60 + y * 90 // h)
    cv2.rectangle(img, (0, int(h * .55)), (w, h), (150, 90, 70), -1)
    # 暗色主体（会被判成"物件"）
    cv2.ellipse(img, (int(w * .45), int(h * .62)), (int(w * .22), int(h * .26)), 0, 0, 360,
                (34, 32, 36), -1)
    # 主体上的细纹理（褶皱感）
    for i in range(28):
        x = int(w * (.25 + .40 * i / 28))
        cv2.line(img, (x, int(h * .40)), (x + 18, int(h * .88)), (58, 55, 60), 2)
    # 亮部：脸部区域 + 耳环硬边
    cv2.circle(img, (int(w * .40), int(h * .26)), int(w * .10), (168, 178, 205), -1)
    cv2.circle(img, (int(w * .47), int(h * .33)), int(w * .035), (210, 215, 225), 3)
    # 发丝（高细节中等梯度 —— 正是"能吃增益"的那一档）
    for i in range(220):
        a = rng.uniform(0, 2 * np.pi)
        r = rng.uniform(.06, .20) * w
        x = int(w * .40 + r * np.cos(a)); y = int(h * .26 + r * np.sin(a) * .8)
        cv2.line(img, (x, y), (x + rng.randint(-9, 10), y + rng.randint(-9, 10)),
                 (48, 46, 52), 1)
    # 一点传感器噪声（真实感；也让"平坦区零改动"这条更严格）
    img = np.clip(img.astype(np.int16) + rng.randint(-2, 3, img.shape), 0, 255).astype(np.uint8)
    return img


def white_product(size=900):
    """模拟白底商品图：大片平坦白 + 少量硬边。判定器应该判它 skip。"""
    img = np.full((size, size, 3), 246, np.uint8)
    cv2.rectangle(img, (int(size * .28), int(size * .20)),
                  (int(size * .72), int(size * .78)), (72, 70, 74), -1)
    for i in range(int(size * .20), int(size * .78), 42):
        cv2.line(img, (int(size * .29), i), (int(size * .71), i), (110, 108, 112), 2)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    a = fashion_portrait()
    b = white_product()
    cv2.imwrite(os.path.join(OUT, "合成_时装人像.png"), a)
    cv2.imwrite(os.path.join(OUT, "合成_白底商品.png"), b)
    print("已生成 ->", OUT)
    for f in sorted(os.listdir(OUT)):
        print("   ", f)
    print("\n这些是**合成图**，不含任何真实素材，可以安全入库。")


if __name__ == "__main__":
    main()
