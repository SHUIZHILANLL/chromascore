# ChromaScore / 音色谱 — 系统设计

> 把音乐的情感色彩量化为随时间演化的色场，**并把「量化强度的代价」做成一等可交互参数**。
>
> 版本：v1.0 —— 已合并为单一程序 `chromascore.py` + 单一入口 `音色谱.bat`
> 输出：`chromascore_report.html`（上半播放器，下半全部曲目卡片，自包含）
>
> 早期拆分的原型（`chromascore_mvp.py` / `analyze_audio.py` / `make_player.py` /
> `make_html.py`）已并入 `chromascore.py`，不再单独保留。本文件保留设计与证据链，
> 作为方法与参数的说明。


---

## 0. 设计立场（先讲清楚，否则会做成又一个频谱动画）

市面上的「音乐可视化」绝大多数是**信号可视化**：频谱、波形、chroma、节拍闪烁。
本系统做的是**感知量可视化**：屏幕上每个像素都由一个情感量（valence / arousal）与一个
演奏表情量（微时序偏差）决定。它回答的不是「声音长什么样」，而是：

> **这段音乐在什么位置、因为什么，失去了它的人味。**

这条主线来自两篇实证工作，它们恰好从相反方向支撑同一个结论：

- **[1] Alton et al. 2025**, *Living Off-Grid: Exploring the Effect of Quantisation on
  Listener Perception of Groove in Hip-Hop Music* — 量化不是中性的技术操作，
  它系统性地改变听众的律动感知。
- **[2] Berdahl et al.**, *Towards Machine Learning of Expressive Microtiming in
  Brazilian Drumming* / *Machine Learning of Expressive Microtiming in Brazilian and
  Reggae Drumming* — 微时序偏差不是噪声，而是可学习、可迁移、可再生成的表情特征。

所以：**情感主要活在偏差里，不在网格上。** 任何以「对齐得更准」为目标的量化，
都会同时把情感表达削掉。系统的全部设计都围绕这条张力展开。

---

## 1. 继承的设计约束（逐条对应证据）

| # | 约束 | 依据 |
|---|---|---|
| C1 | 量化强度 `α` 必须是显式、连续、可交互的参数 | [1]：量化强度本身就是自变量 |
| C2 | 偏差必须按音粒度提取并保留，不能只留统计摘要 | [2]：偏差是可学习的表情载体 |
| C3 | 同时报告偏差分布与 swing ratio，不给单一「律动分」 | [3] Datseris et al. 2021, *Microtiming Deviations and Swing Feel in Jazz* |
| C4 | 情感用连续 (V, A) 表示，不用单一离散标签 | [4] Russell 环形模型；Hevner 8 类环作离散兜底 |
| C5 | VA 曲线按 ≥2 Hz 采样并做短窗平滑 | [5] EMOPIA / awesome-MER / arXiv:2201.05782 的标注惯例 |
| C6 | 输出必须是**时变控制曲线**，而非整曲标量 | [6] Music ControlNet, IEEE/ACM TASLP 2024 |
| C7 | 量化在谱面侧做（出干净网格），时间轴侧保留表演 | [7] arXiv:2508.19262 / madmom / partitura 的双轨做法 |
| C8 | 色彩空间用感知均匀的 OKLCH，不用 HSL | 工程结论：HSL 的 L 不是感知亮度，等步长会出伪影 |

---

## 2. 系统架构

```
                    ┌──────────────┐
   AUDIO (.wav) ───▶│  A. INGEST   │◀─── MIDI (.mid) / MusicXML
                    └──────┬───────┘
                           │ Note[] / audio[]
                    ┌──────▼───────┐
                    │  B. GRID     │  beat / downbeat / 1:16 网格
                    │  madmom      │  → beat_sec, grid_step
                    └──────┬───────┘
              ┌────────────┴────────────┐
       ┌──────▼──────┐          ┌───────▼───────┐
       │ C. DEVIATE  │          │  D. AFFECT    │
       │ microtiming │          │ V/A per beat  │
       │ swing_ratio │          │ 2 Hz 平滑     │
       └──────┬──────┘          └───────┬───────┘
              └────────────┬────────────┘
                    ┌──────▼───────┐
                    │  E. COLOR    │  VA → OKLCH → sRGB
                    │  + α policy  │  α 同时作用于纹理与色阶
                    └──────┬───────┘
              ┌────────────┴────────────┐
       ┌──────▼──────┐          ┌───────▼───────┐
       │ F. RENDER   │          │  G. EMIT      │
       │ PNG / 视频  │          │ JSON 控制曲线 │
       └─────────────┘          └───────────────┘
                    ┌──────▼───────┐
                    │  H. EVALUATE │  情感命中 / 偏差保真 / 人评
                    └──────────────┘
```

MVP 已实现 A（MIDI 子集）、B（自相关估拍）、C、D（无监督调性基线）、E、F、G。

---

## 3. 模块规格

### A. INGEST — 输入归一化

| 项 | 规格 |
|---|---|
| 输入 | SMF (.mid)、MusicXML、音频 |
| 输出 | `Note[] = {onset, dur, pitch, vel}`（秒制） |
| 依赖 | MVP 内置最小 SMF 解析器（零依赖）；v1 接 `mido` + `music21` + `partitura` |
| 出处 | [7]：谱面侧用 partitura，演奏侧用 MIDI/音频 |

**降级顺序**：MusicXML(有真值谱) → MIDI(需对齐) → 音频(需转录，误差最大)。
情感颜色对时间抖动的容忍度远高于谱面对齐，所以音频路线可接受。

### B. GRID — 节拍网格

| 项 | 规格 |
|---|---|
| 输入 | `onset[]` 或音频 |
| 输出 | `beat_sec`, `downbeat[]`, `grid_step = beat_sec/4` |
| MVP 实现 | onset 包络自相关，搜索 0.30–1.20 s |
| v1 实现 | `madmom.features.downbeats.RNNDownBeatProcessor` + `DBNDownBeatTrackingProcessor` |
| 出处 | [7]：arXiv:2508.19262 把 beat/downbeat 作为显式条件，量化质量强依赖它 |

**这是全系统精度瓶颈**，不是情感模型。拍点错 5%，后面所有偏差统计都是错的。

### C. DEVIATE — 微时序偏差提取（系统的灵魂）

```
dev_ms[i] = (onset[i] - round(onset[i] / grid_step) * grid_step) * 1000
```

按 beat 聚合：`dev_ms = mean(|dev|)`，`swing_ratio = (Σdev⁺+1)/(Σ|dev⁻|+1)`。

| 项 | 规格 |
|---|---|
| 输出 | 每 beat 的 `dev_ms`、`swing_ratio`；每音的 `dev_ms` |
| 出处 | [2] 偏差作为表情载体；[3] 双指标报告 |
| 注意 | 网格必须是 1:16。1:8 会吃掉三连音，1:32 会吃掉系统性 swing |

**这里必须同时保留两个粒度**：音粒度（给放大镜视图）与 beat 粒度（给色带）。
只保留 beat 均值就等于丢掉了系统化的提前/延后结构。

### D. AFFECT — 情感量化

| 项 | 规格 |
|---|---|
| 输出 | 每 beat 的 `(V, A) ∈ [0,1]²`，等价采样率 `1/beat_sec`（默认 ≈1.6 Hz） |
| MVP | Krumhansl-Schmuckler major/minor profile 的**相关差** `r_major - r_minor` 作为连续调式量；叠加音区、力度均值/方差、密度、偏差强度 |
| v1 | 音频侧 Essentia mood 模型（`effnet-discogs` / mood 分类器）；符号侧 EMOPIA 微调（MERT 或小的 Transformer） |
| 出处 | [4] Russell 环形；[5] EMOPIA 四象限 / awesome-MER / arXiv:2201.05782 |

为什么 MVP 用「相关差」而不是 `detect_key()` 的 argmax：argmax 是整曲一个标签，
画面会变成一条纯色横条。相关差是连续量，能画出转调处的颜色迁移——这正是
EMOPIA 四象限思路的连续化版本。

> **必须做曲内归一化**。未归一化时 V/A 实测挤在 [0.45, 0.60]，整幅色谱塌成灰绿。
> 归一化后在 `meta.normalised=true` 里如实标注，原始值另存。

### E. COLOR — 情感到色彩

三层，从内到外：

**E1 情感几何**（Russell 环的直接几何化）
```
θ = atan2(A - 0.5, V - 0.5)        # 环形角度
r = min(hypot(V-0.5, A-0.5)/0.7071, 1)   # 情感强度（离心距）
```

**E2 OKLCH**
```
h = (deg(θ) + 180) mod 360                     # 色相
C = 0.012 + 0.27 * r^0.85                      # 彩度 <- 情感强度
L = 0.34 + 0.52 * V                            # 明度 <- valence
```
用 OKLCH 而非 HSL：OKLab 是感知均匀空间，等 L 步长 ≈ 等感知亮度步长。
HSL 在蓝→黄过渡上会突然变暗，色谱会出现假的「凹陷」，被误读成情感事件。

**E3 调性色锚**（文化约定，权重 β=0.18）
```
h = (1-β) * h + β * anchor        # anchor = 42°(大调暖) / 268°(小调冷)
```
β 必须小且可关。Hevner 环与斯克里亚宾调性色彩是**文化联想**，不是感知定律；
当成主映射会把系统变成一张占星表。

**E4 α 策略（本系统的创新点）**

`α` 同时作用在两个通道，因为它们本来就是同一个信息损失的两种表现：

| 通道 | α=0 | α=1 |
|---|---|---|
| 几何：拍内位置 | 实际位置 | 吸附到 1:16 网格 |
| 色彩：色阶数 | 26 档 | 3 档 |
| 色彩：彩度 | 原值 | 衰减 42%（褪色） |

效果：`α=1` 时画面变成整齐的格点 + 大块低饱和色斑——**结构变清楚了，表情死掉了**。
这就是 [1] 的实证结论在屏幕上的一次演示。

### F. RENDER

MVP 输出单张 2000×1296 PNG，四个区域：

1. **三档 α 对比面板**：每个面板散点图，`x=时间`、`y=拍内位置`、格线=1:16 目标
2. **色带**：每 beat 一段，情感色彩随时间的连续演化
3. **microtiming 放大镜**：4 拍窗口，每个音画「实际 → 网格」的连线
4. **Russell 环**：本曲的 VA 轨迹，带起点标记

为什么必须要有第 3 块：全局时间轴上 1 ms ≈ 0.05 px，微时序在主图里根本不可见。
放大镜是唯一能证明「量化到底抹掉了什么」的地方。

### G. EMIT — 数据契约

```jsonc
{
  "meta": {
    "beat_sec": 0.630, "bpm": 95.2, "mode_sign": -1,
    "color_space": "oklch", "deviation_grid_sec": 0.1575,
    "affect_rate_hz": 1.587, "normalised": true
  },
  "cells": [               // 长度 = beat 数，等间隔 —— 直接当控制曲线用
    {"index":0,"t0":0.0,"t1":0.63,"valence":0.31,"arousal":0.22,
     "dev_ms":37.6,"swing_ratio":1.39,"density":3.1,"note_count":2}
  ],
  "notes": [               // 音粒度，给放大镜与再渲染
    {"onset":0.512,"dur":0.283,"pitch":64,"vel":78,"dev_ms":-12.4,"beat_index":0}
  ]
}
```

`cells[]` 是**等间隔时间序列**，与 [6] Music ControlNet 的 time-varying condition
同构，可直接做 `cross-attention` 的条件或 `adaLN` 的调制输入。这是本系统
不只是「画一张图」的地方：图是副产品，控制曲线是主产品。

### H. EVALUATE

见 §6。

---

## 4. 交互设计：α 旋钮是核心，不是装饰

```
        ┌────────────────────────────────────────┐
        │  [曲目] drag&drop                       │
        │                                        │
        │  quantisation strength  α  ═══●═══════ │  ← 唯一的主旋钮
        │  colour anchor β        ══●══════════  │
        │  grid division          1/8 1/16 1/32  │
        │                                        │
        │  [主视图] 散点 + 色带 实时响应 α        │
        │  [放大镜] 4 拍窗口 实时响应 α           │
        │  [读数]   residual|dev| / swing / V / A │
        └────────────────────────────────────────┘
```

拖动 α 时用户看到的因果链是：
**点被钉到格线上 → 残余偏差读数归零 → 色阶被压平、彩度下降 → 整幅画「干净了，也死了」**。

这条因果链是这个产品存在的理由。没有 α，它就只是一张好听的海报。

---

## 5. 路线图

| 阶段 | 交付 | 工作量 | 关键依赖 |
|---|---|---|---|
| **P0** | MVP：MIDI → 三档对比图 + JSON（**已完成**） | 0.5 d | numpy + Pillow |
| **P1** | 接 madmom 真节拍 + 音频输入（librosa→basic-pitch 转录）+ Essentia 情感 | 3 d | madmom, essentia, basic-pitch |
| **P2** | 视频输出：α 随时间扫过，做成 30 s 演示片 | 2 d | imageio-ffmpeg |
| **P3** | Web 实时版：Three.js + WebAudio，α 实时可拖 | 1 w | 前端 + WASM 推理 |
| **P4** | 闭环：把 JSON 曲线喂回 MusicGen/ControlNet 做情感可控生成 | 1–2 w | audiocraft |

P1 的优先级高于 P2/P3：**节拍精度决定一切**，渲染再漂亮也救不回错的网格。

---

## 6. 评估方案

三个正交指标，缺一个就会被自己的图骗：

**6.1 情感命中（模型对不对）**
- VA 回归 vs 人工标注，用 **CCC**（concordance correlation coefficient），不用 Pearson
- 目标：CCC > 0.45（EMOPIA 量级的符号侧合理区间）
- 短片段（<2 s）单独统计，这类样本方差大，别混进总平均

**6.2 偏差保真（量化损失多少）**
- `Wasserstein distance` 比较量化前后 `dev_ms` 分布
- `swing_ratio` 变化率
- 目标：α 从 0→1 时这两个量应该单调变化；如果不是单调，说明偏差提取有 bug

**6.3 感知一致性（颜色对不对）**
- 被试看纯色块，回归出 (V, A)，算与系统映射的 CCC
- 目标 > 0.5
- 这是唯一能验证 E1–E3 映射是否成立的实验，不能用「看起来挺好看」代替

**6.4 端到端范式（对齐 [1]）**
- α ∈ {0, 0.25, 0.5, 0.75, 1} 五档，被试做律动/情绪评分
- 预期：评分随 α 单调下降，且下降曲线在 α≈0.5 之后变陡
- 这个实验本身就是一篇短文

---

## 7. 已知坑位

1. **拍点估计是第一风险**。自相关法在 rubato 段落会漂移；上线前必须换 madmom。
2. **VA 绝对值不可比**。不同曲目间不要直接比色相；曲内归一化后必须标注。
3. **调性色彩联想是文化产物**。β 默认给 0.18 且暴露给用户，不要写死。
4. **1:16 网格不是万能**。三连音、复合拍、自由速度段落会系统性偏离，
   这些段落应单独标记并在 UI 上降透明度，而不是硬套网格。
5. **低饱和区会被误读成「无情感」**。当 V、A 都接近 0.5 时 r→0，颜色变灰。
   这是模型的真实含义（中性），但观众会读成「这一段坏了」。需要图例说明。
6. **色阶 posterise 会提高对比度**，可能让 α=1 看起来比 α=0 更「鲜艳」。
   MVP 用彩度衰减 42% 抵消了这一点，但参数需要重新调。
7. **音频路线的转录误差**会同时污染 beat 与 deviate，误差会相乘。优先 MIDI。

---

## 8. 目录结构（v1 目标）

```
chromascore/
├── chromascore_mvp.py        # P0 单文件 MVP（已完成）
├── chromascore_out.png       # 示例输出
├── chromascore_data.json     # 数据契约实例
├── DESIGN.md                 # 本文件
├── src/
│   ├── ingest.py             # A
│   ├── grid.py               # B  (madmom 封装)
│   ├── deviate.py            # C
│   ├── affect.py             # D  (Essentia / EMOPIA 微调)
│   ├── color.py              # E  (VA→OKLCH)
│   ├── render_static.py      # F1
│   ├── render_video.py       # F2
│   └── emit.py               # G
├── web/                      # P3
└── eval/                     # H  (CCC / Wasserstein / 被试脚本)
```

---

## 9. 参考

1. Alton et al. (2025). *Living Off-Grid: Exploring the Effect of Quantisation on
   Listener Perception of Groove in Hip-Hop Music.*
   https://musicscience.net/wp-content/uploads/2025/11/8-alton-et-al-livingoffgrid-p.-62-71.pdf
2. Berdahl et al. *Towards Machine Learning of Expressive Microtiming in Brazilian Drumming.*
   https://ccrma.stanford.edu/~eberdahl/Projects/Microtiming/ML-Microtiming-ICMC.pdf
3. Datseris et al. (2021). *Microtiming Deviations and Swing Feel in Jazz.* Frontiers in Psychology.
   https://ouci.dntb.gov.ua/en/works/9Qoz20o4/
4. Russell, J. A. (1980). A circumplex model of affect. *(Hevner 1935 离散环作对照)*
5. Hung et al. (2021). *EMOPIA: A Multi-modal Pop Piano Dataset...* arXiv:2108.01374
   https://ar5iv.labs.arxiv.org/html/2108.01374 · 索引 https://github.com/AMAAI-Lab/awesome-MER
6. *Music ControlNet: Multiple Time-Varying Controls for Music Generation.*
   IEEE/ACM TASLP 2024. https://dl.acm.org/doi/abs/10.1109/TASLP.2024.3399026
7. Wachter, Murgul, Heizmann (2025). *Beat-Based Rhythm Quantization of MIDI Performances.*
   arXiv:2508.19262 · 工程侧 https://github.com/CPJKU/madmom · https://github.com/CPJKU/partitura
   泛化侧 https://github.com/craffel/pretty-midi
